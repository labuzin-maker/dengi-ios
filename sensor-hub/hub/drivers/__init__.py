"""Источники датчиков. Чтобы добавить новый — напишите класс-наследник Driver и впишите сюда."""
from .ble import BleDriver
from .demo import DemoDriver
from .shelly import ShellyDriver
from .xiaomi import XiaomiDriver

ALL = {d.name: d for d in (XiaomiDriver, BleDriver, ShellyDriver, DemoDriver)}
