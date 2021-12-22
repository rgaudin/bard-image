#!/usr/bin/env python3

import configparser
import pathlib
import re
import subprocess
import sys

WHITE = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
DHCPCD_CONF_FPATH = pathlib.Path("/etc/dhcpcd.conf")
CF_ENV_PATH = pathlib.Path("/root/bard-content-filter.env")
# # dev
# DHCPCD_CONF_FPATH = pathlib.Path("./dhcpcd.conf")
# CF_ENV_PATH = pathlib.Path("./bard-content-filter.env")

IP_REGEXP = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$")


def is_valid_ip(address):
    match = IP_REGEXP.match(address)
    if not match:
        return False
    for index, sub in enumerate(match.groups()):
        if not sub.isnumeric():
            return False
        dsub = int(sub)
        if dsub < 0 or dsub > 254:
            return False
        if index in (0, 3) and dsub == 0:
            return False
    return True


def log(text: str, color: str = WHITE, end: str = "\n"):
    if color != WHITE:
        print(f"{color}{text}{WHITE}", end=end)
    else:
        print(text, end=end)


def fatal(reason):
    log_error("FAILED")
    log(f"> {reason}")
    return 1


def log_error(text: str):
    log(text, RED)


def write_dhcpcd_conf(network_conf):
    with open(DHCPCD_CONF_FPATH, "r") as fh:
        dhcpcd_conf = fh.read()

    armor_start = "### config-image: start ###"
    armor_end = "### config-image: stop ###"
    lines = dhcpcd_conf.splitlines()
    start = lines.index(armor_start)
    stop = lines.index(armor_end) + 1

    with open(DHCPCD_CONF_FPATH, "w") as fh:
        new_lines = (
            lines[0:start]
            + [armor_start]
            + network_conf.splitlines()
            + [armor_end]
            + lines[stop:]
        )
        if lines[-1] != "":
            new_lines.append("")
        fh.write("\n".join(new_lines))


def main(config_fpath):
    log("Starting device configuration...", end=" ")

    if not config_fpath.exists():
        return fatal(f"Missing config file at {config_fpath}")

    config = configparser.ConfigParser()
    try:
        config.read(config_fpath)
    except Exception as exc:
        return fatal(f"Failed to parse config file: {exc}")

    # missing admin section of password field is just ignored
    if not config.has_section("admin"):
        log("Missing admin section")
    else:
        admin_pw = config["admin"].get("password", "")
        if not admin_pw:
            log("Missing admin password")
        else:
            with open(CF_ENV_PATH, "w") as fh:
                fh.write(f"ADMIN_PASSWORD={admin_pw}\n")

    if not config.has_section("network"):
        return fatal("Missing network section in config file")

    network = config["network"]

    net_type = network.get("type")
    if not net_type:
        return fatal("Missing network type")

    if net_type not in ("dhcp", "static"):
        return fatal(f"Incorrect network type: {net_type}")

    # rest of network conf is solely for static
    if net_type == "static":
        net_addr = network.get("address", "")
        if not net_addr:
            return fatal("Missing static address")
        if not is_valid_ip(net_addr):
            return fatal(f"Incorrect static address: {net_addr}")

        net_routers = network.get("routers", "").split(" ")
        if not net_routers:
            return fatal("Missing static router")
        for router in net_routers:
            if not is_valid_ip(router):
                return fatal(f"Invalid router address: {router}")
        net_dns = network.get("dns", "").split(" ")
        if not net_dns:
            return fatal("Missing static dns")
        for server in net_dns:
            if not is_valid_ip(server):
                return fatal(f"Invalid dns address: {server}")

    if net_type == "dhcp":
        network_conf = "dhcp"
    else:
        network_conf = (
            f"static ip_address={net_addr}/24\n"
            f"static routers={' '.join(net_routers)}\n"
            f"static domain_name_servers={' '.join(net_dns)}\n"
        )

    try:
        write_dhcpcd_conf(network_conf)
    except ValueError as exc:
        return fatal(f"Missing placeholder in {DHCPCD_CONF_FPATH}: {exc}")

    subprocess.run(["/usr/bin/env", "systemctl", "daemon-reload"])
    ps = subprocess.run(["/usr/bin/env", "systemctl", "restart", "dhcpcd5"])
    if ps.returncode != 0:
        return fatal(f"Failed to restart dhcpcd5: exited with {ps.returncode}")
    log("OK", GREEN)
    return 0


if __name__ == "__main__":
    try:
        config_fpath = sys.argv[1]
    except IndexError:
        config_fpath = "/boot/device.conf"
    sys.exit(main(pathlib.Path(config_fpath)))
