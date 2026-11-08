# Getting adb working

`adb` is the one thing Free My Android needs that it is not. It will fetch it
for you:

```sh
./freemyandroid adb get      # downloads platform-tools for this computer
./freemyandroid adb where    # which adb is actually being used
```

It comes from Google's own URL, the same zip the Android documentation points
at, with no account and no SDK manager:

```
https://dl.google.com/android/repository/platform-tools-latest-linux.zip
https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
https://dl.google.com/android/repository/platform-tools-latest-windows.zip
```

It lands in `~/.local/share/freemyandroid/platform-tools/`. Nothing is
installed system-wide and nothing is put on your `PATH`.

**Why the binaries are not committed to this repo.** They are 9–16 MB each,
Google updates them every few weeks, and a checked-in copy would be stale and
subtly wrong by the time anyone cloned it. A URL that always gives the current
version is better than a binary that was current in 2026.

To stage all three — for a USB stick, or to help someone on Windows from a
Linux machine:

```sh
./freemyandroid adb bundle ~/platform-tools
```

Or point the tool at an adb you already have:

```sh
FMA_ADB=/opt/android/platform-tools/adb ./freemyandroid
```

## Linux

```sh
sudo apt install adb          # Debian, Ubuntu
sudo dnf install android-tools # Fedora
sudo pacman -S android-tools   # Arch
```

Two Linux-specific things worth knowing.

**Your user needs permission to talk to the USB device.** If `adb devices`
shows `no permissions`, you are missing a udev rule. The quick check is
whether `sudo adb devices` works where `adb devices` did not. The fix:

```sh
sudo usermod -aG plugdev "$USER"     # then log out and back in
# and install the community rules:
sudo apt install android-sdk-platform-tools-common
```

**Distribution adb is often built without mDNS.** Debian and Ubuntu ship it
this way, so `adb mdns services` returns an empty list rather than an error,
and anything relying on it silently finds nothing. This tool does its own
service discovery for exactly that reason, so it does not matter here — but it
will bite you if you follow a guide elsewhere.

## macOS

```sh
brew install android-platform-tools
```

No driver needed, USB works out of the box. On first connection you may get a
*"Allow accessory to connect"* prompt from macOS as well as the *"Allow USB
debugging?"* prompt on the phone. Both need accepting.

## Windows

This is the awkward one, and it is worth saying plainly: **on Windows, use
wireless debugging and skip the driver problem entirely.** Wireless ADB does
not touch USB, so none of what follows applies.

If you do want USB on Windows, you need a driver that matches the phone, and
Microsoft does not supply one:

- **Google Pixel and AOSP devices** — the *Google USB Driver*, from
  `developer.android.com/studio/run/win-usb`. Install it through Device
  Manager → the phone under *Other devices* → *Update driver* → *Browse*.
- **Samsung** — Samsung's own USB driver, from Samsung's developer site. The
  Google driver does not cover Samsung.
- **Xiaomi, OnePlus, OPPO, vivo, realme** — each OEM publishes its own. The
  generic *WinUSB* driver works for some and not others.
- **Universal ADB Driver** (ClockworkMod) covers many devices in one
  installer. Third-party, so treat it the way you would any driver installer.

A driver problem looks like: the phone charges, Windows makes the connection
sound, and `adb devices` is empty. If `adb devices` lists the phone as
`unauthorized` instead, the driver is fine and you simply have not accepted
the prompt on the phone.

We deliberately do not download OEM Windows drivers for you. They are
per-vendor installers that need administrator rights and change system state,
which is not something a debloating tool should do behind your back.

## The two prompts

**USB.** Settings → About phone → tap *Build number* seven times → Developer
options → *USB debugging*. Plug in, unlock the phone, tick *Always allow from
this computer*, tap *Allow*.

If you never see the prompt and `adb devices` says `unauthorized`:

```sh
adb kill-server && adb start-server
```

and unplug/replug. If it still does not appear, Developer options →
*Revoke USB debugging authorisations*, then reconnect.

**Wireless** (Android 11+). Developer options → *Wireless debugging* → on.
Then:

```sh
./freemyandroid        # Pair with a code, then Find a phone on this network
```

Three things that catch everyone:

1. **The pairing port and the connect port are different numbers.** The
   address under *Pair device with pairing code* is not the one on the main
   Wireless debugging screen. You need the pairing one first, once; after that
   you use the other one forever.
2. **Android stops advertising when the screen sleeps.** Keep the Wireless
   debugging screen open while you connect.
3. **The pairing code expires** and a new one is issued every time the dialog
   opens. If pairing fails, close the dialog, open it again, and read both the
   address and the code fresh.

Many home access points also drop multicast between wireless clients, which
breaks mDNS discovery. This tool falls back to a unicast sweep of your
subnet, so it still finds the phone. If even that fails, use *Type an address
by hand* with the numbers from the phone's screen.
