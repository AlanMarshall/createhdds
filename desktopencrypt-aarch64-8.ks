bootloader --location=mbr --append="console=tty0 quiet"
network --bootproto=dhcp
url --url="https://download.rockylinux.org/pub/rocky/8/BaseOS/aarch64/os/"
lang en_US.UTF-8
keyboard us
timezone --utc America/New_York
clearpart --all
autopart --encrypted --passphrase=weakpassword
rootpw --plaintext weakpassword
user --name=test --password=weakpassword --plaintext --groups=wheel
firstboot --enable
poweroff
text

%packages
@^workstation-product-environment
-selinux-policy-minimum
%end

%addon com_redhat_kdump --enable --reserve-mb='auto'
%end
