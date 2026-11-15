# DHCP configuration examples

PXE clients learn where to boot from DHCP. Your DHCP server has to tell them two things:

- **Next server** (the `siaddr` field, or option 66): the IP of the Docker host running this container.
- **Boot file** (the `file` field, or option 67): which file to download over TFTP.

The boot file depends on the client's firmware, because a legacy BIOS and UEFI can't run each other's binaries:

| Client | Boot file |
| --- | --- |
| UEFI x86_64 | `netboot.xyz-snp.efi` (`netboot.xyz.efi` also works, but can lose the USB keyboard on some PCs) |
| Legacy BIOS | `netboot.xyz.kpxe` |

Only one boot file fits in a DHCP reply. A server that serves both kinds of client has to decide per client, and it does that from **option 93 (Client System Architecture)**, which the PXE firmware sends in its DHCP Discover:

| Option 93 | Client |
| --- | --- |
| `0x0000` | Legacy BIOS x86 |
| `0x0006` | UEFI x86 (32-bit) |
| `0x0007` | UEFI x86_64 (most UEFI PCs) |
| `0x0009` | UEFI x86_64 (sent by some firmware) |
| `0x000b` | UEFI ARM64 |

If every machine you netboot is UEFI, you can skip all of this and hand out `netboot.xyz-snp.efi` to everyone.

> **These are examples, not drop-in configs.** They were written for one network layout, and the syntax and features differ between vendors, software versions and even device models. Interface names, server and pool names, subnets and the netboot host's IP below are placeholders. Check them against your own setup and your device's documentation before you paste anything. netboot.xyz has [networking notes](https://netboot.xyz/docs/category/networking) for some other routers. For anything else, look in your DHCP server's documentation for "PXE", "option 67" or "client architecture".

The examples use:

| | |
| --- | --- |
| LAN subnet | `192.168.1.0/24` |
| Gateway | `192.168.1.1` |
| Docker host running nyxveil-netboot | `192.168.1.10` |

## MikroTik RouterOS 7

RouterOS can pick the boot file per client with a **DHCP option matcher**, which was added in RouterOS 7.4. Legacy BIOS is the default, and the matcher switches UEFI clients over to the EFI file.

```routeros
# 1. The two boot file names (option 67). The inner single quotes make them strings.
/ip dhcp-server option
add name=nxz-bios code=67 value="'netboot.xyz.kpxe'"
add name=nxz-uefi code=67 value="'netboot.xyz-snp.efi'"

# 2. Wrap each in an option set; matchers and servers refer to sets, not options.
/ip dhcp-server option sets
add name=pxe-bios options=nxz-bios
add name=pxe-uefi options=nxz-uefi

# 3. Point clients at the netboot host, and make BIOS the default.
/ip dhcp-server network
set [find address=192.168.1.0/24] next-server=192.168.1.10
/ip dhcp-server
set [find name=dhcp1] dhcp-option-set=pxe-bios

# 4. UEFI x86_64 clients (option 93 = 0x0007 or 0x0009) get the EFI file instead.
/ip dhcp-server matcher
add name=pxe-uefi-x64 server=dhcp1 address-pool=dhcp_pool0 code=93 value="0x0007" option-set=pxe-uefi
add name=pxe-uefi-x64-alt server=dhcp1 address-pool=dhcp_pool0 code=93 value="0x0009" option-set=pxe-uefi
```

Replace `dhcp1` and `dhcp_pool0` with the names from `/ip dhcp-server print` and `/ip pool print`. The option 93 value must be written in hex (`0x0007`); a plain `7` doesn't match.

In WinBox or WebFig, the same settings live under **IP → DHCP Server**: **Options** and **Option Sets** for steps 1 and 2, **Networks** and **DHCP** for step 3, and **Option Matcher** for step 4.

**Checking it.** Boot a client and watch the lease appear under **Leases**. If a UEFI machine still gets `netboot.xyz.kpxe`, capture its Discover (with `/tool sniffer` or Wireshark on the netboot host) and check which option 93 value it actually sends.

## Cisco IOS / IOS XE

The IOS DHCP server can't match on option 93, so a single pool can only hand out one boot file. Pick the file for the clients you have:

```cisco
ip dhcp excluded-address 192.168.1.1 192.168.1.20
!
ip dhcp pool LAN
 network 192.168.1.0 255.255.255.0
 default-router 192.168.1.1
 dns-server 192.168.1.1
 next-server 192.168.1.10
 bootfile netboot.xyz-snp.efi
```

`next-server` and `bootfile` fill the fixed `siaddr` and `file` fields of the reply, which is what PXE firmware reads. If a client insists on the options instead, add them too:

```cisco
ip dhcp pool LAN
 option 66 ascii 192.168.1.10
 option 67 ascii netboot.xyz-snp.efi
```

### Legacy BIOS machines on the same network

If only a few machines need legacy BIOS boot, give each of them a host pool (a manual binding) with the BIOS file. A host pool inherits the gateway, DNS and next server from the network pool it falls inside, so it only needs to override the boot file:

```cisco
ip dhcp pool OLD-PC
 host 192.168.1.50 255.255.255.0
 hardware-address 0011.2233.4455
 bootfile netboot.xyz.kpxe
```

Use the client's MAC address for `hardware-address`. If the binding doesn't match, the client is probably sending a client identifier (option 61); use `client-identifier 0100.1122.3344.55` (`01` followed by the MAC) instead. `show ip dhcp binding` shows how the client is identified.

If you have many BIOS clients, the usual alternatives are to put them on their own VLAN with its own pool, or to stop using the IOS DHCP server for that VLAN: point `ip helper-address` at a DHCP server that can match on option 93 (dnsmasq, Kea, ISC dhcpd, Windows Server).

**Checking it.** `show ip dhcp binding` lists the leases, and `debug ip dhcp server packet` shows each Discover and Offer as it happens. Turn the debug off afterwards with `undebug all`.
