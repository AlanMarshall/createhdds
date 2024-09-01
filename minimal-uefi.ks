bootloader --location=mbr
network --bootproto=dhcp
url --url="https://download.rockylinux.org/pub/rocky/9/BaseOS/x86_64/os/"
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
%end

%addon com_redhat_kdump --enable --reserve-mb='auto'
%end

%post
touch $INSTALL_ROOT/home/home_preserved
%end
