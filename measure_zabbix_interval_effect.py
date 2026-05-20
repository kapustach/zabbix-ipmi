#!/usr/bin/env python3
import csv
import json
import math
import random
import statistics
import subprocess
import time
from datetime import datetime

import requests


ZABBIX_API_URL = "http://10.10.10.10/zabbix/api_jsonrpc.php"
ZABBIX_API_TOKEN = "2c9639ea5beee3282ec973219020633a27ce21f2a74dc16acc8c861736d40c7d"
ZABBIX_AUTH_MODE = "bearer"  # bearer или auth_field

HOST_NAME = "Linux-IPMI"
MASTER_ITEM_NAME_CONTAINS = "Get IPMI sensors"
VALUE_ITEM_NAME_CONTAINS = "CPU Temp, C"

CONTAINER_NAME = "ipmi-sim"
SENSOR_FILE = "/tmp/ipmi/cpu_temp.ipm"

INTERVALS_SEC = [10, 30, 60]
ITERATIONS_PER_INTERVAL = 30

VALUE_NORMAL = 45
VALUE_CHANGED = 55

POLL_API_INTERVAL_SEC = 1
TIMEOUT_EXTRA_SEC = 90
CONFIG_APPLY_WAIT_SEC = 90

CSV_FILENAME = "zabbix_ipmi_interval_reaction.csv"
SUMMARY_FILENAME = "zabbix_ipmi_interval_reaction_summary.csv"


def zabbix_api(method, params):
    headers = {"Content-Type": "application/json-rpc"}
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }

    if ZABBIX_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {ZABBIX_API_TOKEN}"
    else:
        payload["auth"] = ZABBIX_API_TOKEN

    response = requests.post(
        ZABBIX_API_URL,
        headers=headers,
        data=json.dumps(payload),
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise RuntimeError(f"Zabbix API error: {data['error']}")

    return data["result"]


def parse_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        return None


def get_host_id():
    result = zabbix_api(
        "host.get",
        {
            "output": ["hostid", "host", "name"],
            "filter": {"host": [HOST_NAME]},
        },
    )

    if not result:
        result = zabbix_api(
            "host.get",
            {
                "output": ["hostid", "host", "name"],
                "search": {"host": HOST_NAME, "name": HOST_NAME},
                "searchByAny": True,
            },
        )

    if not result:
        raise RuntimeError(f"Хост {HOST_NAME} не найден")

    return result[0]["hostid"]


def find_item(hostid, name_contains):
    result = zabbix_api(
        "item.get",
        {
            "output": ["itemid", "name", "key_", "delay", "lastvalue", "lastclock", "lastns"],
            "hostids": hostid,
            "search": {"name": name_contains},
            "sortfield": "name",
        },
    )

    if not result:
        raise RuntimeError(f"Элемент данных, содержащий '{name_contains}', не найден")

    for item in result:
        if name_contains.lower() in item["name"].lower():
            return item

    return result[0]


def update_item_delay(itemid, interval_sec):
    delay = f"{interval_sec}s"
    zabbix_api(
        "item.update",
        {
            "itemid": itemid,
            "delay": delay,
        },
    )
    print(f"Интервал опроса мастер-элемента изменен на {delay}")


def get_item_state(itemid):
    result = zabbix_api(
        "item.get",
        {
            "output": ["itemid", "name", "lastvalue", "lastclock", "lastns"],
            "itemids": itemid,
        },
    )

    if not result:
        raise RuntimeError(f"Элемент itemid={itemid} не найден")

    return result[0]


def set_sensor_value(value):
    subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER_NAME,
            "sh",
            "-c",
            f"echo {value} > {SENSOR_FILE}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for_value(itemid, target_value, timeout_sec):
    start = time.time()

    while time.time() - start <= timeout_sec:
        state = get_item_state(itemid)
        current_value = parse_float(state.get("lastvalue"))

        if current_value is not None and abs(current_value - float(target_value)) < 0.001:
            return state, time.time()

        time.sleep(POLL_API_INTERVAL_SEC)

    return None, time.time()


def p95(values):
    if not values:
        return None

    values_sorted = sorted(values)
    index = math.ceil(0.95 * len(values_sorted)) - 1
    index = max(0, min(index, len(values_sorted) - 1))
    return values_sorted[index]


def main():
    hostid = get_host_id()

    master_item = find_item(hostid, MASTER_ITEM_NAME_CONTAINS)
    value_item = find_item(hostid, VALUE_ITEM_NAME_CONTAINS)

    print(f"Мастер-элемент: {master_item['name']} itemid={master_item['itemid']}")
    print(f"Измеряемый элемент: {value_item['name']} itemid={value_item['itemid']}")

    all_results = []
    summary_rows = []

    for interval_sec in INTERVALS_SEC:
        print(f"\n=== Интервал опроса {interval_sec} секунд ===")

        update_item_delay(master_item["itemid"], interval_sec)

        print(f"Ожидание применения конфигурации Zabbix: {CONFIG_APPLY_WAIT_SEC} секунд")
        time.sleep(CONFIG_APPLY_WAIT_SEC)

        set_sensor_value(VALUE_NORMAL)
        baseline_state, _ = wait_for_value(
            value_item["itemid"],
            VALUE_NORMAL,
            timeout_sec=interval_sec + TIMEOUT_EXTRA_SEC,
        )

        if baseline_state is None:
            raise RuntimeError(f"Zabbix не зафиксировал исходное значение {VALUE_NORMAL}")

        print(f"Исходное значение зафиксировано: {baseline_state.get('lastvalue')}")

        interval_delays = []

        for attempt in range(1, ITERATIONS_PER_INTERVAL + 1):
            random_pause = random.uniform(1, max(2, interval_sec))
            time.sleep(random_pause)

            target_value = VALUE_CHANGED if attempt % 2 == 1 else VALUE_NORMAL

            before_state = get_item_state(value_item["itemid"])
            before_value = before_state.get("lastvalue")

            injection_epoch = time.time()
            injection_time = datetime.fromtimestamp(injection_epoch).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            set_sensor_value(target_value)

            detected_state, detected_epoch = wait_for_value(
                value_item["itemid"],
                target_value,
                timeout_sec=interval_sec + TIMEOUT_EXTRA_SEC,
            )

            if detected_state is None:
                row = {
                    "interval_sec": interval_sec,
                    "attempt": attempt,
                    "target_value": target_value,
                    "before_value": before_value,
                    "after_value": "",
                    "injection_time": injection_time,
                    "detected_time": "",
                    "reaction_sec": "",
                    "status": "timeout",
                }
                all_results.append(row)
                print(f"{attempt:02d}: timeout")
                continue

            detected_time = datetime.fromtimestamp(detected_epoch).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            reaction_sec = round(detected_epoch - injection_epoch, 3)
            interval_delays.append(reaction_sec)

            row = {
                "interval_sec": interval_sec,
                "attempt": attempt,
                "target_value": target_value,
                "before_value": before_value,
                "after_value": detected_state.get("lastvalue"),
                "injection_time": injection_time,
                "detected_time": detected_time,
                "reaction_sec": reaction_sec,
                "status": "ok",
            }
            all_results.append(row)

            print(
                f"{attempt:02d}: {before_value} -> {target_value}; "
                f"реакция = {reaction_sec} с"
            )

        if interval_delays:
            summary = {
                "interval_sec": interval_sec,
                "count": len(interval_delays),
                "min": round(min(interval_delays), 3),
                "max": round(max(interval_delays), 3),
                "avg": round(statistics.mean(interval_delays), 3),
                "median": round(statistics.median(interval_delays), 3),
                "p95": round(p95(interval_delays), 3),
            }
        else:
            summary = {
                "interval_sec": interval_sec,
                "count": 0,
                "min": "",
                "max": "",
                "avg": "",
                "median": "",
                "p95": "",
            }

        summary_rows.append(summary)

    with open(CSV_FILENAME, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "interval_sec",
            "attempt",
            "target_value",
            "before_value",
            "after_value",
            "injection_time",
            "detected_time",
            "reaction_sec",
            "status",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(all_results)

    with open(SUMMARY_FILENAME, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = ["interval_sec", "count", "min", "max", "avg", "median", "p95"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(summary_rows)

    print("\nСводные результаты:")
    print("Интервал; Количество; Минимум; Максимум; Среднее; Медиана; P95")
    for row in summary_rows:
        print(
            f"{row['interval_sec']}; {row['count']}; {row['min']}; "
            f"{row['max']}; {row['avg']}; {row['median']}; {row['p95']}"
        )

    print(f"\nФайл измерений сохранен: {CSV_FILENAME}")
    print(f"Файл сводной статистики сохранен: {SUMMARY_FILENAME}")


if __name__ == "__main__":
    main()
