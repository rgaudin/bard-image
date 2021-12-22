#!/usr/bin/env python3

""" prepare a “base” image for Bard with our 3 partition scheme and ZIM in 3rd part

Requirements:

apt-get update -y && \
apt-get install -y --no-install-recommends fdisk parted exfat-utils curl unzip
curl -L -o /tmp/qemu-5.2.0-linux-x86_64.tar.gz \
http://mirror.download.kiwix.org/dev/qemu-5.2.0-linux-x86_64.tar.gz && \
cd /usr/local/bin && tar xvf /tmp/qemu-5.2.0-linux-x86_64.tar.gz && \
chmod +x qemu-* && cd -
"""

import collections
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

###############
# Constants
###############
RASPIOS_URI = urllib.parse.urlparse(
    "https://downloads.raspberrypi.org/raspios_oldstable_lite_armhf/images/"
    "raspios_oldstable_lite_armhf-2021-12-02/2021-12-02-raspios-buster-armhf-lite.zip"
)
RASPIOS_FNAME = Path(RASPIOS_URI.path).with_suffix(".img").name
ONE_GB = int(1e9)
ONE_GiB = 2 ** 30
MOUNT_ON = Path("./data_volume")
REPO_URL = "https://raw.githubusercontent.com/rgaudin/bard-image/main"

###############
# Customize
###############
MIN_SYSTEM_SIZE = 2 * ONE_GB
SYSTEM_SIZE = 7 * ONE_GB
REQUESTED_IMAGE_SIZE = 32 * ONE_GB  # will be converted to power of 2
DATA_PARTITION_LABEL = "DATA"
ZIM_URL = os.getenv(
    "ZIM_URL",
    "http://mirror.download.kiwix.org/zim/stack_exchange/"
    "beer_stackexchange_com_2021-12.zim",
)
KEEP_ZIM_COPY_HERE = True
###############


PartitionBoundaries = collections.namedtuple(
    "PartitionBoundaries", ["root_start", "root_end", "data_start", "data_end"]
)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("bard-prep")


# download raspiOS
def download_raspios(workdir: Path):
    raspios_fpath = workdir / RASPIOS_FNAME
    master_fpath = workdir / "master.img"
    if master_fpath.exists():
        return master_fpath
    if not raspios_fpath.exists():
        zip_fpath = workdir / Path(RASPIOS_URI.path).name
        subprocess.run(
            ["curl", "-L", "-o", zip_fpath, "-C", "-", RASPIOS_URI.geturl()], check=True
        )
        subprocess.run(["unzip", "-d", workdir, zip_fpath], check=True)

    shutil.copyfile(raspios_fpath, master_fpath)

    return master_fpath


def download_or_copy_zim(workdir: Path, mount_point):
    zim_path = Path(urllib.parse.urlparse(ZIM_URL).path)
    zim_fname = zim_path.name
    zim_name = zim_path.stem

    # touch a test file
    mount_point.joinpath("test").touch()

    target = mount_point / zim_fname
    if target.exists():
        return

    local = workdir / zim_fname
    if local.exists():
        logger.debug(f"Copying {local.name} into {target.parent}")
        shutil.copyfile(local, target)

    logger.debug(f"Downloading from {ZIM_URL}")
    subprocess.run(["curl", "-L", "-o", str(target), "-C", "-", ZIM_URL], check=True)

    if KEEP_ZIM_COPY_HERE:
        logger.debug(f"Copying downloaded {target.name} into {local.parent}")
        shutil.copyfile(target, local)

    logger.debug("Write ZIM_NAME to env file")
    with open(mount_point / "bard-reverse-proxy.env", "w") as fh:
        fh.write(
            "# name of ZIM used for redirection from /kiwix\n"
            "# doesn't require date suffix as kiwix automatically includes that\n"
            f"ZIM_NAME={zim_name}\n"
        )

    logger.debug("Write empty urls.json as placeholder")
    with open(mount_point / "urls.json", "w") as fh:
        fh.write("[]")

    logger.debug("sync")
    sync()
    return zim_fname


def read_remote(url):
    with urllib.request.urlopen(url) as response:
        return response.read().decode("UTF-8")


def as_power_of_2(size):
    """round to the next nearest power of 2"""
    return 2 ** math.ceil(math.log(size, 2))


def get_qemu_adjusted_image_size(size):
    """number of bytes to resize image file to to accomodate Qemu

    which expects it to be a power of 2 (integer)"""

    # if size is not a rounded GiB multiple, round it to next power of 2
    return size if size % ONE_GiB == 0 else as_power_of_2(size)


def resize_image(fpath: Path, size: int, shrink: Optional[bool] = False):
    args = (
        ["qemu-img", "resize"]
        + (["--shrink"] if shrink else [])
        + ["-f", "raw", str(fpath.resolve()), str(size)]
    )
    subprocess.run(args, check=True)


def get_virtual_device(image_fpath):
    """create and return a loop device or drive letter we can format/mount"""

    # find out offset for third partition from the root part size

    # prepare loop device
    loop_maker = subprocess.run(
        [
            "/sbin/losetup",
            "--find",
            "--show",
            image_fpath,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=True,
    ).stdout.strip()

    target_dev = re.search(r"(\/dev\/loop[0-9]+)\.?$", loop_maker).groups()[0]

    return target_dev


def release_virtual_device(device):
    subprocess.run(["/sbin/losetup", "--detach", device])


def format_data_partition(device):
    subprocess.run(
        ["/sbin/mkfs.exfat", "-n", DATA_PARTITION_LABEL, f"{device}p3"], check=True
    )


def mount_data_partition(device, mount_point):
    MOUNT_ON.mkdir(exist_ok=True)
    subprocess.run(
        ["mount", "-t", "exfat", f"{device}p3", str(mount_point)], check=True
    )


def unmount_data_partition(mount_point):
    MOUNT_ON.mkdir(exist_ok=True)
    time.sleep(2)  # prevent some failures
    subprocess.run(["umount", str(mount_point)])


def get_partitions_boundaries(lines, root_size, disk_size):

    if isinstance(lines, str):
        lines = lines.splitlines()

    sector_size = 512
    round_bound = 128
    end_margin = 4194304  # 4MiB

    def roundup(sector):
        return rounddown(sector) + round_bound if sector % round_bound != 0 else sector

    def rounddown(sector):
        return sector - (sector % round_bound) if sector % round_bound != 0 else sector

    # parse all lines
    number_of_sector_match = []
    second_partition_match = []
    target_reg = (
        r"[0-9a-zA-Z\.\-\_]+\.img"
        if ".img" in "\n".join(lines)
        else r"\/dev\/[0-9a-z]+"
    )
    for line in lines:
        number_of_sector_match += re.findall(
            r"^Disk {}:.*, (\d+) sectors$".format(target_reg), line
        )
        second_partition_match += re.findall(
            r"^{}\d +(\d+) +(\d+) +\d+ +\S+ +\d+ +Linux$".format(target_reg), line
        )

    # ensure we retrieved nb of sectors correctly
    if len(number_of_sector_match) != 1:
        raise ValueError("cannot find the number of sector of disk")
    number_of_sector = int(number_of_sector_match[0])

    # ensure we retrieved the start of the root partition correctly
    if len(second_partition_match) != 1:
        raise ValueError("cannot find start and/or end of root partition of disk")
    second_partition_start = int(second_partition_match[0][0])
    second_partition_end = int(second_partition_match[0][1])

    # whether disk is already full
    is_full = second_partition_end + 1 == number_of_sector
    if is_full:
        pass  # whether root part was already expanded

    size_up_to_root_b = root_size
    nb_clusters_endofroot = size_up_to_root_b // sector_size

    # align partitions (otherwise exfat-fuse gets often corrupt)
    root_start = second_partition_start
    root_end = roundup(nb_clusters_endofroot)

    data_start = root_end + 1

    # data_end = number_of_sector (using full avail space)

    # end second partition on a predicatble cluster
    # full_size - root_size (root_end) - margin
    data_bytes = disk_size - root_size - end_margin
    data_clusters = data_bytes // sector_size
    data_end = data_start + data_clusters

    return PartitionBoundaries(root_start, root_end, data_start, data_end)


def partprobe(device):
    subprocess.run(["partprobe", "-s", device])


def fdisk(device: str, commands: str, check: bool = False, probe: bool = False):
    subprocess.run(
        ["fdisk", device], universal_newlines=True, input=commands, check=check
    )
    if probe:
        partprobe(device)


def sync():
    subprocess.run(["sync"])


def expand_rootfs(device, boundaries: PartitionBoundaries):
    nb_partitions = len(
        [
            line
            for line in get_partition_table(device).splitlines()
            if line.startswith(device)
        ]
    )

    print(f"nb_partitions={nb_partitions}")

    if nb_partitions >= 3:
        logger.debug("delete data partition (exists)")
        fdisk(device, "d\n3\nw", probe=True)

    logger.debug("recreating root partition")
    commands = f"""d
2
n
p
2
{boundaries.root_start}
{boundaries.root_end}
t
2
83
w"""
    fdisk(device, commands, probe=True)

    logger.debug("resize filesystem on root partition")
    time.sleep(5)
    subprocess.run(["resize2fs", f"{device}p2"], check=True)


def get_partition_table(device):
    return subprocess.run(
        ["fdisk", "-l", device],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=True,
    ).stdout


def create_third_partition(device, boundaries):
    commands = f"""n
p
3
{boundaries.data_start}
{boundaries.data_end}
t
3
7
w"""
    fdisk(device, commands, probe=True)


def update_boot_partition(device, mount_point):
    logger.debug("Mount boot partition")
    mount_point.mkdir(exist_ok=True)
    subprocess.run(["mount", "-t", "vfat", f"{device}p1", str(mount_point)], check=True)

    logger.debug("fix cmdline.txt")
    with open(mount_point / "cmdline.txt", "r") as fh:
        cmdline = fh.read()
    with open(mount_point / "cmdline.txt", "w") as fh:
        fh.write(
            cmdline.replace(
                "init=/usr/lib/raspi-config/init_resize.sh", "init=/sbin/init"
            )
        )

    logger.debug("write dhcp network conf")
    with open(mount_point / "device.conf", "w") as fh:
        fh.write(read_remote(f"{REPO_URL}/device.conf"))

    logger.debug("write config device script")
    config_script = mount_point / "config-device.py"
    with open(config_script, "w") as fh:
        fh.write(read_remote(f"{REPO_URL}/config-device.py"))
    config_script.chmod(0o755)

    sync()
    subprocess.run(["umount", str(mount_point)])


def get_shrunk_size():

    # get ZIM size looking at curl's returned headers
    zim_size = int(
        [
            line
            for line in subprocess.run(
                ["curl", "-I", ZIM_URL],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            ).stdout.splitlines()
            if line.startswith("Content-Length:")
        ][-1]
        .strip()
        .split(":", 1)[-1]
    )

    required_image_size = SYSTEM_SIZE + zim_size + (ONE_GB / 4)
    return math.ceil(required_image_size / ONE_GB) * ONE_GB


def main(target_folder: str = ".", *args):
    if sys.platform != "linux":
        raise NotImplementedError("Linux-only :/")

    # convert sizes to bytes and make sure those are usable
    try:
        IMAGE_SIZE = get_qemu_adjusted_image_size(REQUESTED_IMAGE_SIZE)

        if SYSTEM_SIZE < MIN_SYSTEM_SIZE:
            raise ValueError(
                "root partition must be at least {}".format(MIN_SYSTEM_SIZE)
            )

        if SYSTEM_SIZE >= IMAGE_SIZE:
            raise ValueError("root partition must be smaller than disk size")
    except Exception as exp:
        logger.error("Erroneous size option: {}".format(exp))
        return 1

    workdir = Path(target_folder)
    logger.info(f"Starting with workdir={str(workdir)}")

    logger.info("Fetching raspiOS master")
    master_fpath = download_raspios(workdir)
    logger.info("> OK")

    # resize image
    logger.info(f"Resize base image to {IMAGE_SIZE}")
    resize_image(master_fpath, IMAGE_SIZE)
    logger.info("> OK")

    # setup loop device
    logger.info("Setting-up a virtual loop device")
    device = get_virtual_device(master_fpath)
    partprobe(device)
    logger.info(f"> OK at {device}")

    # retrieve initial partition table
    part_table = get_partition_table(device)
    logger.info(f"Initial partition table:\n{part_table}")
    # Analyze disk partition table
    boundaries = get_partitions_boundaries(part_table, SYSTEM_SIZE, IMAGE_SIZE)
    logger.info(f"boundaries={boundaries}")

    logger.info("Expand rootfs")
    expand_rootfs(device, boundaries)
    logger.info("> OK")

    print(get_partition_table(device))

    # create third partition leaving space for system part grow
    logger.info("Add third partition at end of disk")
    create_third_partition(device, boundaries)
    logger.info("> OK")

    # format third partition as exfat
    logger.info("Format third partition")
    format_data_partition(device)
    logger.info("> OK")

    # mount third partition
    logger.info("Mount third partition")
    unmount_data_partition(MOUNT_ON)
    mount_data_partition(device, MOUNT_ON)
    logger.info("> OK")

    # copy files into third partition
    logger.info("Copy files into third partition")
    download_or_copy_zim(workdir=workdir, mount_point=MOUNT_ON)
    logger.info("> OK")

    # unmount
    logger.info("Unmount third partition")
    unmount_data_partition(MOUNT_ON)
    logger.info("> OK")

    logger.info("Fix boot partition")
    update_boot_partition(device, MOUNT_ON.with_name("boot_volume"))
    logger.info("> OK")

    # release device
    logger.info(f"Release virtual device ({device})")
    release_virtual_device(device)
    logger.info("> OK")

    logger.info("Shrink image to minimal size")
    resize_image(master_fpath, get_shrunk_size(), shrink=True)
    logger.info("> OK")

    logger.info("ALL DONE. Time to start image and run in-system script.")


if __name__ == "__main__":
    try:
        sys.exit(main(*sys.argv[1:]))
    except Exception as exc:
        logger.error(f"ERROR: {exc}\n\n-----")
        logger.exception(exc)
        sys.exit(1)
