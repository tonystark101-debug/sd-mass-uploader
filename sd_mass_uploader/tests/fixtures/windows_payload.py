"""Windows inventory for one laptop, one powered hub and four readers.

Shapes copied from real output of INVENTORY_SCRIPT on Windows 11; the devices are invented for
tests. Reader A has a card, B is empty, C has an unmounted card, D has no disk record at all.
"""

from __future__ import annotations

from src.device_manager.usb_topology import WindowsPort

GUID = "#{f18a0e88-c30c-11d0-8815-00a0c906bed8}"
ROOT_HUB = f"\\\\?\\USB#ROOT_HUB30#5&AAA&0&0{GUID}"
EXT_HUB = f"\\\\?\\USB#VID_05E3&PID_0626#6&BBB&0&1{GUID}"
HUBS = [ROOT_HUB, EXT_HUB]


def port(hub, number, vid, pid, bcd, speed, driver, is_hub=False, flags=0, child=None):
    return WindowsPort(hub, number, vid, pid, bcd, speed, is_hub, flags, driver, child)


PORTS = [
    port(ROOT_HUB, 1, "05e3", "0626", 0x0320, 3, "{hub}\\0001", True, 0x3, EXT_HUB),
    port(EXT_HUB, 1, "0bda", "0307", 0x0300, 3, "{usb}\\0001", flags=0x3),
    port(EXT_HUB, 2, "0bda", "0307", 0x0300, 3, "{usb}\\0002", flags=0x3),
    port(EXT_HUB, 3, "0bda", "0307", 0x0300, 2, "{usb}\\0003"),  # 3.0 reader stuck at 480.
    port(EXT_HUB, 4, "0bda", "0307", 0x0300, 3, "{usb}\\0004", flags=0x3),
]


def _reader(serial):
    return {"DeviceID": f"USB\\VID_0BDA&PID_0307\\{serial}", "Name": "USB Mass Storage Device",
            "Manufacturer": "Compatible USB storage device", "Service": "USBSTOR",
            "CompatibleID": ["USB\\Class_08&SubClass_06&Prot_50", "USB\\Class_08"], "Status": "OK"}


def _prop(instance, key, data):
    return {"InstanceId": instance, "KeyName": key, "Data": data}


def _disk(index, serial, model, size, loaded, media):
    return {"Index": index, "DeviceID": f"\\\\.\\PHYSICALDRIVE{index}", "Model": model,
            "InterfaceType": "USB", "MediaType": media, "MediaLoaded": loaded, "Size": size,
            "PNPDeviceID": f"USBSTOR\\DISK&VEN_GENERIC&PROD_STORAGE\\{serial}&0",
            "SerialNumber": None, "Status": "OK"}


PAYLOAD = {
    "errors": [],
    "usb_pnp": [
        {"DeviceID": "USB\\ROOT_HUB30\\5&AAA&0&0", "Name": "USB Root Hub (USB 3.0)",
         "Manufacturer": "(Standard USB HUBs)", "Service": "USBHUB3", "CompatibleID": None,
         "Status": "OK"},
        {"DeviceID": "USB\\VID_05E3&PID_0626\\6&BBB&0&1", "Name": "Generic SuperSpeed USB Hub",
         "Manufacturer": "Generic", "Service": "USBHUB3", "CompatibleID": ["USB\\Class_09"],
         "Status": "OK"},
        _reader("SERIAL_A"), _reader("SERIAL_B"), _reader("SERIAL_C"), _reader("SERIAL_D"),
    ],
    "pnp_properties": [
        _prop("USB\\VID_05E3&PID_0626\\6&BBB&0&1", "DEVPKEY_Device_Driver", "{hub}\\0001"),
        _prop("USB\\VID_0BDA&PID_0307\\SERIAL_A", "DEVPKEY_Device_Driver", "{usb}\\0001"),
        _prop("USB\\VID_0BDA&PID_0307\\SERIAL_B", "DEVPKEY_Device_Driver", "{usb}\\0002"),
        _prop("USB\\VID_0BDA&PID_0307\\SERIAL_C", "DEVPKEY_Device_Driver", "{usb}\\0003"),
        _prop("USB\\VID_0BDA&PID_0307\\SERIAL_D", "DEVPKEY_Device_Driver", "{usb}\\0004"),
        _prop("USBSTOR\\DISK&VEN_GENERIC&PROD_STORAGE\\A&0", "DEVPKEY_Device_Parent",
              "USB\\VID_0BDA&PID_0307\\SERIAL_A"),
        _prop("USBSTOR\\DISK&VEN_GENERIC&PROD_STORAGE\\B&0", "DEVPKEY_Device_Parent",
              "USB\\VID_0BDA&PID_0307\\SERIAL_B"),
        _prop("USBSTOR\\DISK&VEN_GENERIC&PROD_STORAGE\\C&0", "DEVPKEY_Device_Parent",
              "USB\\VID_0BDA&PID_0307\\SERIAL_C"),
    ],
    "disk_drives": [
        {"Index": 0, "DeviceID": "\\\\.\\PHYSICALDRIVE0", "Model": "SAMSUNG NVMe",
         "InterfaceType": "SCSI", "MediaType": "Fixed hard disk media", "MediaLoaded": True,
         "Size": 512105932800, "PNPDeviceID": "SCSI\\DISK&VEN_NVME\\5&1&0&0",
         "SerialNumber": "0025_3841.", "Status": "OK"},
        _disk(1, "A", "Generic STORAGE DEVICE USB Device", 63_000_000_000, True,
              "Removable Media"),
        _disk(2, "B", "Generic STORAGE DEVICE USB Device", None, False, "Removable Media"),
        _disk(3, "C", "Generic STORAGE DEVICE USB Device", 31_000_000_000, True,
              "Removable Media"),
    ],
    "disks": [
        {"Number": 0, "FriendlyName": "SAMSUNG", "SerialNumber": None, "Size": 512110190592,
         "IsOffline": False, "IsReadOnly": False, "BusType": "NVMe", "OperationalStatus": "Online"},
        {"Number": 1, "FriendlyName": "Generic", "SerialNumber": None, "Size": 63_000_000_000,
         "IsOffline": False, "IsReadOnly": False, "BusType": "USB", "OperationalStatus": "Online"},
        {"Number": 2, "FriendlyName": "Generic", "SerialNumber": None, "Size": 0,
         "IsOffline": False, "IsReadOnly": False, "BusType": "USB",
         "OperationalStatus": "No Media"},
        {"Number": 3, "FriendlyName": "Generic", "SerialNumber": None, "Size": 31_000_000_000,
         "IsOffline": False, "IsReadOnly": False, "BusType": "USB", "OperationalStatus": "Online"},
    ],
    "partitions": [
        {"DiskNumber": 0, "PartitionNumber": 3, "Size": 340720426496, "DriveLetter": "C",
         "AccessPaths": ["C:\\", "\\\\?\\Volume{c-drive}\\"]},
        {"DiskNumber": 1, "PartitionNumber": 1, "Size": 63_000_000_000, "DriveLetter": "F",
         "AccessPaths": ["F:\\", "\\\\?\\Volume{card-a}\\"]},
        {"DiskNumber": 3, "PartitionNumber": 1, "Size": 31_000_000_000, "DriveLetter": "",
         "AccessPaths": ["\\\\?\\Volume{card-c}\\"]},
    ],
    "volumes": [
        {"DeviceID": "\\\\?\\Volume{c-drive}\\", "DriveLetter": "C:", "Label": "OS",
         "FileSystem": "NTFS", "Capacity": 340720422912, "FreeSpace": 88793874432,
         "SerialNumber": 1484276862, "DriveType": 3, "Name": "C:\\"},
        {"DeviceID": "\\\\?\\Volume{card-a}\\", "DriveLetter": "F:", "Label": "CAM_A",
         "FileSystem": "exFAT", "Capacity": 62_900_000_000, "FreeSpace": 1_000_000,
         "SerialNumber": 0x1A2B3C4D, "DriveType": 2, "Name": "F:\\"},
        {"DeviceID": "\\\\?\\Volume{card-c}\\", "DriveLetter": None, "Label": "CAM_C",
         "FileSystem": "exFAT", "Capacity": 30_900_000_000, "FreeSpace": 500,
         "SerialNumber": 0x55667788, "DriveType": 2, "Name": "\\\\?\\Volume{card-c}\\"},
    ],
}
