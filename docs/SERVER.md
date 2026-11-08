# Running services on an Android phone

An old phone is a surprisingly good little server. It has an ARM SoC, 4–8 GB
of RAM, flash storage, Wi-Fi, and — the part no single-board computer gives you
— a battery, which is a UPS that happens to be built in. What it does not have
is a kernel willing to run containers.

This document is the honest version of what works.

## The short answer

**You cannot run Docker on an unrooted Android phone.** Not with a trick, not
with a flag. `dockerd` has to create kernel namespaces, write cgroups and
mount overlay filesystems, and Android grants an ordinary app none of those
three. An app runs as an unprivileged uid in a SELinux domain that forbids all
of it.

Rooting helps less than people expect. Termux's `docker` package lives in
`root-repo`, has been pinned to 20.10.12 since 2021, and container start still
fails on kernels missing the cgroup layout `runc` wants. Getting native Docker
genuinely working means compiling a custom kernel with the right namespace and
cgroup options and flashing it.

So: four paths, none of which is "install Docker".

## The four paths

### 1. Termux + proot-distro — start here

A Debian or Ubuntu userland running under PRoot, no containers involved. You
run the service directly, the way you would on a VPS in 2012.

```sh
pkg install proot-distro
proot-distro install debian
proot-distro login debian
```

**Best for anything that is one process and a port.** Stirling PDF is the
example you asked about, and it is a good one: it is a Java/Spring
application, so it is just a JRE and a jar.

```sh
# inside the Debian guest
apt update && apt install -y openjdk-21-jre-headless
# Stirling PDF, without Docker
wget https://github.com/Stirling-Tools/Stirling-PDF/releases/latest/download/Stirling-PDF.jar
java -jar Stirling-PDF.jar --server.port=8080
```

Also fine this way: nginx, Caddy, Node, Python web apps, FreshRSS, Gitea,
Syncthing, a static site, Vaultwarden.

What you give up: no image registry, no `docker compose`, no isolation, and
you resolve dependency conflicts yourself. PRoot intercepts syscalls with
`ptrace`, so anything syscall-heavy — compiling, for instance — runs at
perhaps half speed. Serving HTTP is not syscall-heavy and barely notices.

### 2. udocker — real OCI images, no root, no VM

[udocker](https://github.com/indigo-dc/udocker) is a userspace
reimplementation of the Docker *client* experience. It pulls real images from
a registry, unpacks them, and runs them under PRoot. It is now in Termux's own
APT repo, and [`George-Seven/Termux-Udocker`](https://github.com/George-Seven/Termux-Udocker)
(★408) carries the configuration that makes it behave, plus working setups for
Jellyfin, Home Assistant and Puter.

```sh
pkg install udocker
udocker pull debian:bookworm
udocker run debian:bookworm
```

Use it when the thing you want only ships as an image and you do not fancy
reproducing it by hand.

What you give up: it is PRoot underneath, so it is slow and there is no real
isolation — "container" here means "unpacked filesystem with a faked root". No
Compose, no container-to-container networking, no volumes in the Docker sense.
udocker's own manual says Termux supports only the `P` (PRoot) execution mode,
and recommends using Termux's own `proot` binary:

```sh
export UDOCKER_USE_PROOT_EXECUTABLE=$(command -v proot)
```

### 3. Android 16 Linux Terminal — the real thing, on the right hardware

Android 16 ships a genuine Debian VM through the Android Virtualization
Framework, on protected KVM. Root inside the guest, systemd, `apt`, disk
resize, port forwarding, GPU acceleration. **Docker actually runs in here**,
because it is a real kernel.

Settings → About phone → tap *Build number* ×7 → Developer options →
**Linux development environment**.

The catch is hardware. It needs *protected* VM support (pKVM), which means in
practice: Pixels, some Exynos Samsungs, and not a great deal else. Samsung
often disables AVF entirely. Nothing's CMF Phone 2 Pro fails outright on the
Dimensity 7300 Pro with "Non-protected VMs are not supported on this device".

`./freemyandroid info` reports what it can work out:

```
Linux VM (AVF): supported | unprotected-only | unsupported
```

Treat `supported` as "probably": the properties say the framework is there,
and an OEM can still have left pKVM out underneath.

If you have the hardware, this is the best option by a wide margin, and
[`nix-community/nixos-avf`](https://github.com/nix-community/nixos-avf) will
even put NixOS in it.

### 4. QEMU in Termux — real dockerd, slowly

Boot an Alpine guest under QEMU inside Termux and run a real `dockerd` in it,
exposing the socket back to the host. It works. Everyone who has tried it uses
the word "slow", and they are being kind — this is full emulation of a machine
on a phone SoC.

A `termux-qemu-docker` package, described by its author as "a VM manager like
Lima/Colima but focused on running Docker in QEMU in Termux without root", was
proposed as termux-packages issue #30165 in June 2026. If that lands, this path
gets a lot more pleasant.

## Keeping it alive — the part people miss

A service that starts and then dies twenty minutes later is the usual
experience, and it is not your configuration. Android is actively trying to
stop you. Three mechanisms, all of which `./freemyandroid` can switch off
through the **Developer / server** profile:

**The phantom process killer.** Android 12 added a watchdog that kills an
app's *child* processes once it has more than a handful. Termux is one app with
many children, so this shoots your server and logs nothing you will find.

```
settings put global settings_enable_monitor_phantom_procs false
```

This is the single most important setting on the phone. Nothing else on this
page matters if it is on.

**Doze.** With the screen off, the system suspends background work in
increasingly long windows. Termux needs an exemption:

```
cmd deviceidle whitelist +com.termux
```

**Sleep on the charger.** A phone that dozes drops off the network.

```
settings put global stay_on_while_plugged_in 3
```

Then, on the phone itself: Termux's notification has an **Acquire wakelock**
action — use it. Install **Termux:Boot** so a reboot does not end the
experiment, and put your start script in `~/.termux/boot/`. Note that Termux
and its add-ons must come from the **same source**, because they have to share
a signing key; this tool fetches both from F-Droid for exactly that reason.

**Battery.** Leave it on the charger and it will sit at 100% and swell within
a year or two. If the phone supports a charge limit, use it. Otherwise a smart
plug on a schedule, or accept that the battery is consumable.

## Reaching it

Do not forward a port from your router to a phone. Two better options:

- **Tailscale** — in the app catalogue (`./freemyandroid apps`). The phone
  joins your tailnet and the service is reachable by name from anywhere, with
  no open ports and no dynamic DNS.
- **Cloudflare Tunnel** — `cloudflared` runs fine in the proot guest if you
  want something on the public internet with a real certificate.

## The web panel — designed, not yet built

You asked for something like Dokploy: a page you open, pick a service, press
start. Every existing panel in that space — Dokploy, CasaOS, Portainer,
Umbrel, Cosmos — is built on Docker, so all of them are dead on arrival here.
Which is the argument for building one, not for giving up.

**`fmaserve`** — the shape it should take:

- **One Python file, standard library only**, running inside the proot guest.
  Same rule as the rest of this project: no pip, no node, no build step. It
  has to survive being installed over a flaky mobile connection by someone who
  will not debug it.
- **Recipes, not images.** A recipe is JSON: a name, an install script, a
  start command, a port, and a health check. `data/recipes/stirling-pdf.json`
  does for Stirling PDF what a compose file would.
- **Process supervision without systemd.** proot has no PID 1 worth the name.
  Start detached, write a PID file, poll it. Restart on exit with a backoff.
  Keep the last N KB of stdout per service in a ring buffer so the Logs tab
  has something to show.
- **The UI is one page.** A list of services, each with a state dot, a port
  link, and Start / Stop / Logs. An *Available* tab listing recipes not yet
  installed. No framework, no bundler.
- **Bound to the Tailscale interface by default**, not `0.0.0.0`. A panel that
  can start processes should not be reachable from the whole LAN by accident.

Recipes worth shipping first, in order of how well they actually run on a
phone: Stirling PDF, a static site via Caddy, FreshRSS, Syncthing, Gitea,
Uptime Kuma, Jellyfin (audio only — transcoding video on a phone is not a
good time).

Open question, and it is a real one: whether the panel should manage proot
services, udocker containers, or both behind one interface. Both is nicer and
roughly doubles the work. Starting with proot only, and adding udocker as a
second backend once the supervision is solid, is the plan unless there is a
reason to do otherwise.
