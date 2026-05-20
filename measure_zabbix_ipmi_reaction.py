#!/usr/bin/env python3
import json
import statistics
import subprocess
import time
from datetime import datetime

import requests
import csv

ZABBIX_API_URL = "http://10.10.10.10/zabbix/api_jsonrpc.php"
ZABBIX_API_TOKEN = "2c9639ea5beee3282ec973219020633a27ce21f2a74dc16acc8c861736d40c7d"

HOST_NAME = "Linux-IPMI"
ITEM_NAME = "CPU Temp, C"

CONTAINER_NAME = "ipmi-sim"
SENSOR_FILE = "/tmp/ipmi/cpu_temp.ipm"

POLL_INTERVAL_SEC = 1
TIMEOUT_SEC = 120

ITERATIONS = 30

TEST_VALUES = []
for i in range(ITERATIONS):
    if i % 2 == 0:
        TEST_VALUES.append(55)
    else:
        TEST_VALUES.append(45)


def zabbix_api(method, params):
    headers = {
        "Content-Type": "application/json-rpc",
        "Authorization": f"Bearer {ZABBIX_API_TOKEN}",
    }

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }

    response = requests.post(
        ZABBIX_API_URL,
        headers=headers,
        data=json.dumps(payload),
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise RuntimeError(f"Zabbix API error: {data['error']}")

    return data["result"]


def get_item_id():
    result = zabbix_api(
        "item.get",
        {
            "output": ["itemid", "name", "key_", "lastvalue", "lastclock", "lastns"],
            "host": HOST_NAME,
            "search": {
                "name": ITEM_NAME
            },
            "sortfield": "name",
        },
    )

    if not result:
        raise RuntimeError(f"Item '{ITEM_NAME}' was not found on host '{HOST_NAME}'")

    for item in result:
        if item["name"] == ITEM_NAME:
            return item["itemid"]

    return result[0]["itemid"]


def get_item_state(itemid):
    result = zabbix_api(
        "item.get",
        {
            "output": ["itemid", "name", "lastvalue", "lastclock", "lastns"],
            "itemids": itemid,
        },
    )

    if not result:
        raise RuntimeError(f"Item ID '{itemid}' was not found")

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
    )


def zabbix_timestamp(item):
    clock = int(item.get("lastclock", "0") or 0)
    ns = int(item.get("lastns", "0") or 0)
    return clock + ns / 1_000_000_000


def main():
    itemid = get_item_id()
    print(f"Item found: {ITEM_NAME}, itemid={itemid}")

    results = []

    for index, target_value in enumerate(TEST_VALUES, start=1):
        before = get_item_state(itemid)
        before_value = before.get("lastvalue")
        before_zbx_ts = zabbix_timestamp(before)

        injection_time = time.time()
        set_sensor_value(target_value)

        detected = None

        while time.time() - injection_time <= TIMEOUT_SEC:
            current = get_item_state(itemid)
            current_value = current.get("lastvalue")
            current_zbx_ts = zabbix_timestamp(current)

            try:
                current_value_float = float(str(current_value).replace(",", "."))
            except ValueError:
                current_value_float = None

            if (
                current_value_float is not None
                and abs(current_value_float - float(target_value)) < 0.001
                and current_zbx_ts >= int(injection_time)
            ):
                detected = current
                break

            time.sleep(POLL_INTERVAL_SEC)

        if detected is None:
            print(f"{index}: target={target_value}, timeout")
            results.append({
                "attempt": index,
                "target": target_value,
                "status": "timeout",
            })
            continue

        detection_time = time.time()
        reaction_by_api = detection_time - injection_time
        reaction_by_zabbix_clock = zabbix_timestamp(detected) - injection_time

        row = {
            "attempt": index,
            "target": target_value,
            "before_value": before_value,
            "after_value": detected.get("lastvalue"),
            "injection_time": datetime.fromtimestamp(injection_time).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "detected_time": datetime.fromtimestamp(detection_time).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "zabbix_lastclock": detected.get("lastclock"),
            "zabbix_lastns": detected.get("lastns"),
            "reaction_api_sec": round(reaction_by_api, 3),
            "reaction_zabbix_clock_sec": round(reaction_by_zabbix_clock, 3),
            "status": "ok",
        }
        results.append(row)

        print(
            f"{index}: {before_value} -> {target_value}, "
            f"API reaction={row['reaction_api_sec']} s, "
            f"Zabbix clock reaction={row['reaction_zabbix_clock_sec']} s"
        )

        time.sleep(3)

    ok_results = [r for r in results if r.get("status") == "ok"]
    delays = [r["reaction_api_sec"] for r in ok_results]

    print("\nCSV:")
    print("attempt,target,before_value,after_value,injection_time,detected_time,zabbix_lastclock,zabbix_lastns,reaction_api_sec,reaction_zabbix_clock_sec,status")
    for r in results:
        print(
            f"{r.get('attempt','')},"
            f"{r.get('target','')},"
            f"{r.get('before_value','')},"
            f"{r.get('after_value','')},"
            f"{r.get('injection_time','')},"
            f"{r.get('detected_time','')},"
            f"{r.get('zabbix_lastclock','')},"
            f"{r.get('zabbix_lastns','')},"
            f"{r.get('reaction_api_sec','')},"
            f"{r.get('reaction_zabbix_clock_sec','')},"
            f"{r.get('status','')}"
        )
    csv_filename = "zabbix_ipmi_reaction_30_iterations.csv"

    with open(csv_filename, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
                "attempt",
                "target",
                "before_value",
                "after_value",
                "injection_time",
                "detected_time",
                "zabbix_lastclock",
                "zabbix_lastns",
                "reaction_api_sec",
                "reaction_zabbix_clock_sec",
                "status",
                ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for r in results:
            writer.writerow({
                "attempt": r.get("attempt", ""),
                "target": r.get("target", ""),
                "before_value": r.get("before_value", ""),
                "after_value": r.get("after_value", ""),
                "injection_time": r.get("injection_time", ""),
                "detected_time": r.get("detected_time", ""),
                "zabbix_lastclock": r.get("zabbix_lastclock", ""),
                "zabbix_lastns": r.get("zabbix_lastns", ""),
                "reaction_api_sec": r.get("reaction_api_sec", ""),
                "reaction_zabbix_clock_sec": r.get("reaction_zabbix_clock_sec", ""),
                "status": r.get("status", ""),
                })
    print(f"\nResults saved to: {csv_filename}")
    
    if delays:
        delays_sorted = sorted(delays)
        p95_index = int(0.95 * (len(delays_sorted) - 1))
        p95 = delays_sorted[p95_index]

        print("\nSummary:")
        print(f"count={len(delays)}")
        print(f"min={min(delays):.3f} s")
        print(f"max={max(delays):.3f} s")
        print(f"avg={statistics.mean(delays):.3f} s")
        print(f"median={statistics.median(delays):.3f} s")
        print(f"p95={p95:.3f} s")


if __name__ == "__main__":
    main()
