bootloader --location=mbr
network --bootproto=dhcp
url --url="https://download.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/"
#repo --name="epel" --baseurl="http://mirrors.kernel.org/fedora-epel/9/Everything/x86_64/"
# use epel to keep scsi-target-utils instead of targetcli
lang en_US.UTF-8
keyboard us
timezone --utc America/New_York
clearpart --all
autopart
rootpw weakpassword
poweroff
text

%packages
@core
targetcli
nfs-utils
dnsmasq
%end

%addon com_redhat_kdump --enable --reserve-mb='auto'
%end
