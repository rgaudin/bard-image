#!/bin/bash

if [ "$EUID" -ne 0 ]
  then echo "Please run as root"
  exit
fi

# update clock
echo "update clock"
timedatectl --adjust-system-clock set-ntp 1

REPO_URL="https://raw.githubusercontent.com/rgaudin/bard-image/main"

# make sure to use exfat module and that it is loaded on start
echo "ensure exfat in modules"
exfat_in_modules=$(cat /etc/modules-load.d/modules.conf | grep exfat | wc -l)
if [ "$exfat_in_modules" = "0" ];
then
    echo "exfat" | tee -a /etc/modules-load.d/modules.conf
fi

echo "ensure exfat is loaded"
exfat_loaded=$(cat /proc/filesystems |grep exfat | wc -l)
if [ "$exfat_loaded" = "0" ];
then
    modprobe exfat
fi

# add data partition to exfat and mount
echo "ensure /data in fstab"
mkdir -p /data
part3_present=$(cat /etc/fstab |grep exfat | wc -l)
if [ "$part3_present" = "0" ];
then
    prefix=$(cat /etc/fstab |grep ext4 | cut -d "-" -f1)
    echo "${prefix}-03 /data           exfat   umask=0002,uid=1000,gid=33,x-systemd.device-timeout=3min  0       0" | tee -a /etc/fstab
    mount -a
fi

# make sure we have this file as it will be bind-mounted
echo "[]" > /data/urls.json

echo "pi-bard" > /etc/hostname

echo "install default (dhcp) network conf with script placeholder"
wget -O /etc/dhcpcd.conf $REPO_URL/dhcpcd.conf

echo "Add config script to rc.local"
wget -O /etc/rc.local $REPO_URL/rc.local

# install balenaEngine
echo "install balena-engine"
curl -sL https://github.com/balena-os/balena-engine/releases/download/v18.9.13/balena-engine-v18.9.13-armv7hf.tar.gz | tar xzv -C /usr/local/bin/ --strip-components=2
groupadd balena-engine
# Add files listed bellow
wget -O /etc/systemd/system/balena.service $REPO_URL/balena.service
wget -O /etc/systemd/system/balena.socket $REPO_URL/balena.socket

systemctl daemon-reload
systemctl start balena.socket
systemctl enable balena.socket
systemctl start balena
systemctl enable balena
usermod -aG balena-engine pi  # (or whatever user) to enable non-root balena mgmt

ln -s /var/run/balena-engine.sock /var/run/docker.sock

# install docker-compose
curl -L "https://github.com/docker/compose/releases/download/v2.2.2/docker-compose-linux-armv7" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

echo "test docker-compose"
docker-compose --version

echo "install compose"
wget -O /root/docker-compose.yml $REPO_URL/docker-compose.yml
docker-compose -f /root/docker-compose.yml up -d
