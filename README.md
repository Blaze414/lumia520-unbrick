# lumia520-unbrick

Recovers a **Nokia Lumia 520 (RM-914 / RM-915, Qualcomm MSM8227)** stuck in
Qualcomm emergency download mode (**`QHSUSB_DLOAD`**, USB `05c6:9008`) from
**macOS**, without Windows, thor2 or a JTAG box.

It rewrites the phone's bootloader chain from a stock FFU ROM, so the phone
boots back into Nokia flash mode. The full OS flash (stage 3) still needs
Windows (thor2 or Windows Device Recovery Tool).

```
QHSUSB_DLOAD (05c6:9008)  ──stage 1──▶  QHSUSB_ARMPRG (05c6:9008)  ──stage 2──▶  NOKIA FLASH (0421:066e)  ──stage 3 (Windows)──▶  Windows Phone
   boot ROM, DMSS              programmer, streaming          Lumia UEFI flash app                    booting phone
```

## Status

Tested once, end to end, on a retail **RM-915**:

| Step | Result |
|---|---|
| Probe boot ROM | `PBL_DloadVER2.0`, root key hash read over DMSS `0x18` |
| Root key checks (phone vs programmer vs FFU SBL1) | all three matched |
| Stage 1: programmer upload + GO | phone re-enumerated as `QHSUSB_ARMPRG` |
| Stage 2: 9 regions written, every packet ACKed | ✓ |
| Reboot | phone came up as `NOKIA FLASH` (`0421:066e`) |
| Stage 3: full FFU flash | not done by this tool (Windows) |

Files used: `FAST8930_RM915.hex` and
`RM915_3058.51000.1505.0012_RETAIL_…_prd_signed.ffu`.

## Quick start

```bash
brew install libusb
python3 -m venv .venv && .venv/bin/pip install pyusb
```

```bash
.venv/bin/python lumia_emergency.py selftest
```

```bash
.venv/bin/python lumia_emergency.py probe
```

```bash
.venv/bin/python lumia_emergency.py info --hex FAST8930_RM915.hex --ffu RM915_xxx.ffu
```

```bash
.venv/bin/python lumia_emergency.py flash --hex FAST8930_RM915.hex --ffu RM915_xxx.ffu
```

```bash
.venv/bin/python lumia_emergency.py flash --hex FAST8930_RM915.hex --ffu RM915_xxx.ffu --yes
```

`flash` without `--yes` runs every check and stops. Only `--yes` writes.

Then, on Windows with the phone in `NOKIA FLASH` mode:

```
thor2 -mode uefiflash -ffufile RM915_xxx.ffu -do_full_nvi_update -do_factory_reset -reboot
```

### Commands

| Command | Touches phone | Writes | Does |
|---|---|---|---|
| `selftest` | no | no | CRC, HDLC, Intel HEX, FFU, GPT, plan and root key parser tests on synthetic data |
| `probe` | yes | no | DMSS NOP, version string, root key hash |
| `info` | no | no | parses HEX + FFU, prints root keys and the write plan |
| `flash` | yes | no | `info` + reads phone root key, compares all three |
| `flash --yes` | yes | **yes** | stage 1 + stage 2, then reboots the phone |

## Inputs

| File | What it is | Where it comes from |
|---|---|---|
| `FAST8930_RM915.hex` (or `RM914`) | Nokia **V1 emergency programmer**, Intel HEX, signed with the phone's root key | Nokia Care emergency packages; shared unofficially |
| `RM915_*.ffu` | Stock **Full Flash Update** ROM for the product code | Microsoft/Nokia ROM servers (e.g. via Lumia firmware archives) |

`RM915_prg_v1.0.hex` is also a V1 programmer (it runs an eMMC read/write self-test
in thor2). `RM915_msimage_v1.0.mbn` is **not needed**: the same boot partitions are
taken directly from the FFU.

## Hardware and protocol specs

| Item | Value |
|---|---|
| SoC | Qualcomm MSM8227 (Snapdragon S4 Play), FFU platform ID `Nokia.MSM8227.P6036` |
| Storage | eMMC, 512-byte sectors, GPT |
| Emergency mode USB | `05c6:9008`, product string `QHSUSB_DLOAD` |
| Programmer mode USB | `05c6:9008`, product string `QHSUSB_ARMPRG` |
| Lumia flash mode USB | `0421:066e`, product string `NOKIA FLASH` |
| Boot ROM protocol | Qualcomm **DMSS download** (HDLC framed), version `PBL_DloadVER2.0` |
| Programmer protocol | Qualcomm **streaming download** ("QCOM fast download protocol") |
| Programmer load / entry address | `0x2A000000` |
| eMMC open-multi partition id | `0x21` (user area, raw) |
| Address unit in stream writes | **bytes**, little endian, 32-bit |

### HDLC framing

```
0x7E | payload | CRC16 (LE) | 0x7E      0x7D/0x7E in body escaped as 0x7D, byte^0x20
```

CRC is CRC-16/X.25 (poly `0x8408` reflected, init `0xFFFF`, xorout `0xFFFF`,
check value `0x906E`).

* **Stage 1 (boot ROM):** commands are framed, replies are framed.
* **Stage 2 (V1 programmer):** commands are sent **raw** (unframed),
  replies are still framed. This matches WPinternals (`EncodeCommands = false`).

### Stage 1: DMSS commands (boot ROM)

Multi-byte fields are **big endian**.

| Cmd | Name | Payload | Reply |
|---|---|---|---|
| `0x06` | NOP | — | `0x02` ACK |
| `0x0C` | SW version | — | `0x0D` len, ASCII |
| `0x18` | Root key hash | — | `0x18 0x01 0x00` + 32-byte SHA-256 |
| `0x0F` | Write 32-bit | addr (4) · len (2) · data (≤ `0x100`) | `0x02` |
| `0x05` | GO | addr (4) | `0x02`, then USB re-enumeration |

### Stage 2: streaming commands (programmer)

Multi-byte fields are **little endian**.

| Cmd | Name | Payload | Reply |
|---|---|---|---|
| `0x01` | Hello | `"QCOM fast download protocol host"` (32) · `02 02 01` | `0x02` |
| `0x17` | Security mode | `0x00` | `0x18` |
| `0x1B` | Open multi-image | `0x21` | `0x1C` |
| `0x07` | Stream write | byte addr (4) · data (≤ `0x400`) | `0x08` · same addr |
| `0x15` | Close | — | `0x16` |
| `0x0B` | Reset | — | `0x0C` (may drop off USB first) |

## Architecture

Single file, [`lumia_emergency.py`](lumia_emergency.py), standard library plus
`pyusb`. Layers, bottom up:

| Layer | Functions / classes | Notes |
|---|---|---|
| Framing | `crc16`, `frame`, `unframe` | own implementation; validated against a real reply from the phone |
| USB | `Phone` | finds `05c6:9008`, waits for a given product string, claims interface 0, bulk IN/OUT; `command()` sends one packet and checks the reply prefix |
| Stage 1 | `dmss_alive`, `dmss_version`, `dmss_rkh`, `dmss_upload`, `dmss_go` | 256-byte write chunks |
| Stage 2 | `HELLO`, `flash_write` | 1 KiB raw writes, every reply's echoed address is checked |
| Files | `parse_hex`, `root_key_hash`, `FFU`, `parse_gpt` | read-only; FFU is streamed by sector, never loaded whole |
| Plan | `build_plan` | the list of `(name, byte offset, data)` to write |
| CLI | `cmd_probe`, `cmd_info`, `cmd_flash`, `cmd_selftest` | `IOError`/`ValueError` exit with `error: …` |

### Intel HEX parsing

Data (`00`), extended linear (`04`) and extended segment (`02`) records, line
checksums verified. The image must be contiguous and start at `0x2A000000`,
and must contain the UTF-16LE string `QHSUSB_ARMPRG` (marks a V1 programmer,
same rule as WPinternals).

### Root key hash

Scan the binary for chained DER certificates (`30 82 LL LL 30 82`); the root
key hash is SHA-256 of the **last** certificate in the first chain. Applied to
the programmer and to the FFU's `SBL1` partition, and compared with the hash
the boot ROM reports. An all-zero phone hash (engineering device) skips the
comparison.

### FFU parsing

```
[Security header + catalog + hash table]  "SignedImage "   rounded up to chunk size
[Image header + manifest]                 "ImageFlash  "   rounded up to chunk size
[Store header + validate + write descriptors]              rounded up to chunk size
[Payload: chunks]
```

* Chunk size = security header `+0x10` × 1 KiB (128 KiB on Lumia ROMs).
* Store header: platform ID at `+0x0C` (192 bytes), write descriptor count `+208`,
  write descriptor length `+212`, validate descriptor length `+220`.
* Each write descriptor: `location count`, `chunk count`, then per location
  `(disk access method, disk chunk index)`. Method `0` = from start of disk;
  method `2` (end of disk, backup GPT) is ignored.
* Disk chunks not present in the FFU read as zeros.
* File size must equal headers + chunk count × chunk size, otherwise the FFU is
  rejected as truncated. Short sector reads also abort.

### Write plan

Same regions and order as WPinternals' emergency-mode bootloader restore. Offsets
below are from the tested RM-915 FFU:

| # | Region | Byte offset | Size | Source |
|---|---|---|---|---|
| 1 | MBR | `0x00000000` | 512 | FFU sector 0 |
| 2 | GPT | `0x00000200` | 16 895 | FFU sectors 1–33, **last byte dropped** (programmer bounds-check bug) |
| 3 | SBL2 | `0x01200000` | 1 536 000 | FFU partition |
| 4 | SBL3 | `0x01400000` | 2 097 152 | FFU partition |
| 5 | UEFI | `0x01600000` | 2 560 000 | FFU partition |
| 6 | SBL1 | `0x01000000` | 1 535 488 | FFU partition, **last sector dropped** |
| 7 | TZ | `0x01C00000` | 512 000 | FFU partition |
| 8 | RPM | `0x01A00000` | 512 000 | FFU partition |
| 9 | WINSECAPP | `0x01E00000` | 523 776 | FFU partition, **clamped to end at `0x1E7FE00`** |

About 9 MB total. The boot ROM itself lives in mask ROM and is never written,
so a failed stage 2 leaves the phone in `QHSUSB_DLOAD` again and the tool can be
re-run. If the phone is already in `QHSUSB_ARMPRG`, `flash` skips stage 1.

## Safety checks (before anything is written)

* Programmer root key == phone root key.
* FFU `SBL1` root key == phone root key (catches RM-914/RM-915 or wrong-model ROMs).
* FFU platform ID contains `MSM8227`.
* FFU file size consistent (not truncated).
* GPT has all of `SBL1 SBL2 SBL3 UEFI TZ RPM WINSECAPP`, none of `HACK` /
  `IS_UNLOCKED_SBL3`, no partition placed after its `BACKUP_` twin, and all
  end before sector `0xF400` (V1 programmer limit). Non-stock layouts are refused
  instead of rewritten.
* Every region fits the 32-bit byte address space.
* Every write reply must echo the exact address that was sent.
* Nothing is written without `--yes`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no 05c6:9008 device found` | cable, port or hub; check `ioreg -p IOUSB` for `QHSUSB_DLOAD` |
| `root key mismatch` | programmer or FFU is for a different model or region; get matching RM-91x files |
| `no reply to command 06` | boot ROM wedged; hold Power ~10 s (or remove the battery) and reconnect |
| programmer never appears as `QHSUSB_ARMPRG` | wrong or corrupt HEX; try the other V1 programmer |
| reply mismatch during stage 2 | re-run; the tool resumes from `QHSUSB_ARMPRG`. Persistent failures can mean a failing eMMC |
| after reboot still `QHSUSB_DLOAD` | SBL chain rejected (wrong ROM) or eMMC failure |

## Limitations

* Only the **V1** programmer protocol (`QHSUSB_ARMPRG`) is supported. V2
  (`QHSUSB__BULK`, Sahara/Firehose) phones are out of scope; use
  [bkerler/edl](https://github.com/bkerler/edl) for those.
* Stage 3 (full FFU flash over the Nokia UEFI flash protocol) is not implemented.
* No read-back verification: the V1 programmer has no eMMC read command, so
  success is judged by per-packet ACKs and by the phone booting to flash mode.
* Tested on one RM-915. Other MSM8227 / MSM8x30 Lumias (620, 625, 720) use the
  same flow in WPinternals but have not been tried with this tool.

## Credits

Protocol details, the load address, partition id `0x21`, the write list and its
workarounds are ported from **[WPinternals](https://github.com/ReneLergner/WPinternals)**
by Rene Lergner (MIT): `QualcommDownload.cs`, `QualcommFlasher.cs`,
`QualcommSerial.cs`, `FFU.cs`, `QualcommPartition.cs`,
`LumiaUnlockBootloaderViewModel.cs`. See [LICENSE](LICENSE).

## Disclaimer

This writes the bootloader of a phone. Use it only on a device you own, with
files matching its exact product code. No warranty.
