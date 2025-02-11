from asyncio.streams import StreamReader, StreamWriter
from dotenv import load_dotenv
import asyncio
import os
import json
import re
from datetime import datetime
import paho.mqtt.client as mqtt
import logging
from typing import Awaitable, Callable, Union


load_dotenv()


class Config:
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "")
    P1_ADDRESS = os.getenv("P1_ADDRESS", "")
    P1_PORT = int(os.getenv("P1_PORT", 2000))
    MQTT_BROKER = os.getenv("MQTT_BROKER", "")
    MQTT_TOPIC = os.getenv("MQTT_TOPIC", "")
    MQTT_USER = os.getenv("MQTT_USER", "")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
    INTERVAL = int(os.getenv("INTERVAL", 5))


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
    level=logging.DEBUG if Config.LOG_LEVEL == "DEBUG" else logging.INFO,
)


obis: list = json.load(open(os.path.join(os.path.dirname(__file__), "obis.json")))[
    "obis_fields"
]
mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(Config.MQTT_USER, Config.MQTT_PASSWORD)
mqtt_client.connect(Config.MQTT_BROKER, 1883, 60)
mqtt_client.loop_start()


def calc_crc(telegram: list[bytes]) -> str:
    telegram_str = b"".join(telegram)
    telegram_cut = telegram_str[0 : telegram_str.find(b"!") + 1]
    x = 0
    y = 0
    crc = 0
    while x < len(telegram_cut):
        crc = crc ^ telegram_cut[x]
        x = x + 1
        y = 0
        while y < 8:
            if (crc & 1) != 0:
                crc = crc >> 1
                crc = crc ^ (int("0xA001", 16))
            else:
                crc = crc >> 1
            y = y + 1
    return hex(crc)


def parse_hex(str) -> str:
    try:
        result = bytes.fromhex(str).decode()
    except ValueError:
        result = str
    return result


async def send_telegram(telegram: list[bytes]) -> None:
    def format_value(value: str, type: str, unit: str) -> Union[str, float]:
        # Timestamp has message of format "YYMMDDhhmmssX"
        multiply = 1
        if (len(unit) > 0 and unit[0]=='k'):
            multiply = 1000
        format_functions: dict = {
            "float": lambda str: float(str) * multiply,
            "int": lambda str: int(str) * multiply,
            "timestamp": lambda str: int(
                datetime.strptime(str[:-1], "%y%m%d%H%M%S").timestamp()
            ),
            "string": lambda str: parse_hex(str),
            "unknown": lambda str: str,
        }
        return_value = format_functions[type](value.split("*")[0])
        return return_value

    telegram_formatted: dict = {}
    line: str
    for line in [line.decode() for line in telegram]:
        matches: list[[]] = re.findall("(^.*?(?=\\())|((?<=\\().*?(?=\\)))", line)
        if len(matches) > 0:
            obis_key: str = matches[0][0]
            obis_item: Union[dict, None] = next(
                (item for item in obis if item.get("key", "") == obis_key), None
            )
            if obis_item is not None:
                item_type: str = obis_item.get("type", "")
                #logging.debug("Key %s  Name: %s Unit: %s <-- %s" %  (obis_item.get("key", "") ,  obis_item.get("name", "") , obis_item.get("unit", "no unit") , line. strip()) )
                unit = obis_item.get("unit", "no unit")
                item_value_position: Union[int, None] = obis_item.get("valuePosition")
                telegram_formatted[obis_item.get("name")] = (
                    format_value(matches[1][1], item_type, unit )
                    if len(matches) == 2
                    else (
                        "|".join(
                            [
                                str(
                                    format_value(
                                        match[1],
                                        item_type[index]
                                        if type(item_type) == list
                                        else item_type,
                                        unit
                                    )
                                )
                                for index, match in enumerate(matches[1:])
                            ]
                        )
                        if item_value_position is None
                        else format_value(
                            matches[2][1],
                            item_type[item_value_position],
                            unit
                        )
                    )
                )
    try:
        result = mqtt_client.publish(
            Config.MQTT_TOPIC,
            payload=json.dumps(telegram_formatted),
            retain=True,
        )
        if result.rc == 0:
            logging.info("Telegram published on MQTT")
        else:
            logging.error(f"Telegram not published (return code {result.rc})")
    except Exception as err:
        logging.error(f"Unable to publish telegram on MQTT: {err}")


async def process_lines(reader):
    telegram: Union[list, None] = None
    iteration_limit: int = 10
    i: int = 0
    while True:
        if i > iteration_limit:
            raise Exception(f"Exceeded iteration limit: {iteration_limit} iteration(s)")
        data: bytes = await reader.readline()
        logging.debug(data)
        if data.startswith(b"/"):
            telegram = []
            i = i + 1
            logging.debug("New telegram")
        if telegram is not None:
            telegram.append(data)
            if data.startswith(b"!"):
                crc: str = hex(int(data[1:], 16))
                calculated_crc: str = calc_crc(telegram)
                if crc == calculated_crc:
                    logging.info(f"CRC verified ({crc}) after {i} iteration(s)")
                    await send_telegram(telegram)
                    break
                else:
                    raise Exception("CRC check failed")


async def read_telegram():
    reader: StreamReader
    writer: StreamWriter
    reader, writer = await asyncio.open_connection(Config.P1_ADDRESS, Config.P1_PORT)
    try:
        await process_lines(reader)
    except Exception as err:
        logging.debug(err)
    finally:
        writer.close()


async def read_p1():
    async def timeout(awaitable: Callable, timeout: float) -> Union[Awaitable, None]:
        try:
            return await asyncio.wait_for(awaitable(), timeout=timeout)
        except Exception as err:
            logging.error(
                f"Unable to read data from {Config.P1_ADDRESS}: {str(err) or err.__class__.__name__}"
            )

    while True:
        logging.info("Read P1 reader")
        await asyncio.gather(
            asyncio.sleep(Config.INTERVAL),
            timeout(read_telegram, timeout=10),
        )


if __name__ == "__main__":
    asyncio.run(read_p1())
