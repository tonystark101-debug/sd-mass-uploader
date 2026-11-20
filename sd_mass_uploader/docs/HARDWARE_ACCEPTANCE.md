# Physical Acceptance Tests

Mocked tests cannot prove this works with your hubs, readers and cards. Complete these on **both**
Windows and Ubuntu before uploading real recordings. The master plan requires Stages 1 and 2 to
pass before full-scale uploading.

Use test cards with data you can afford to lose, or copies. Record the results in the tables.

## Stage 1: Detection

Start small, then scale: one reader, two, five, ten, all, then all hubs at full load.

| Step | Do this | Pass when |
| --- | --- | --- |
| 1.1 | `python main.py check` with one reader and a card | One reader `ACTIVE`, correct capacity and filesystem |
| 1.2 | Take the card out, run `check` | `EMPTY_READER` or `READER_PRESENT_SLOT_UNKNOWN`. Note which |
| 1.3 | Unplug the reader, run `check` | Reader shows `DISCONNECTED`, no crash |
| 1.4 | Connect all readers | Reader count equals the real count. Hubs are not counted as readers |
| 1.5 | Compare each link speed with Microsoft USBView (Windows) or `lsusb -t` (Ubuntu) | Speeds agree |
| 1.6 | Move one reader to a USB 2 port | Speed drops to 480 Mbps |
| 1.7 | Use a card with no partition table | The volume appears and is offered |
| 1.8 | Run `monitor`, then add and remove a card | Both changes are reported |
| 1.9 | Change a card's drive letter or mount point, then rerun `scan` | Same card ID |
| 1.10 | Run the automated checks below | All pass |

```bash
EXPECT_READERS=18 python -m pytest tests/hardware -m hardware -s --override-ini addopts=""
```

On Windows PowerShell, set the variable first with `$env:EXPECT_READERS = 18`.

If a reader's name does not identify it as a card reader, it is marked for confirmation. Send the
model name so it can be added to the recognized list.

| Date | OS | Hub model | Reader model | Readers | Result | Notes |
| --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |

## Stage 2: File scanning

| Step | Do this | Pass when |
| --- | --- | --- |
| 2.1 | `python main.py scan --files` on a card with a known file count | Count and bytes match the OS file properties |
| 2.2 | Card with Hindi or other non-English file names | No errors |
| 2.3 | Pull a card during a scan | That card shows `DISCONNECTED`, others finish |
| 2.4 | Hash sample files before and after (`certutil -hashfile` or `sha256sum`) | Hashes and modified times are identical |
| 2.5 | On Ubuntu, try `touch` on a card mounted read-only, then scan | The scan is unaffected |

## Stage 2B: Station mode (Ubuntu)

Run `python main.py daemon` in one terminal, and `python main.py station status` in another.

| Step | Do this | Pass when |
| --- | --- | --- |
| 2B.1 | Insert a card that the desktop has not mounted | It mounts read-only within a few seconds (`findmnt` shows `ro`) and appears as `SCANNING` |
| 2B.2 | With the desktop's auto-mount on, insert a card | The station uses the existing mount and does not mount it twice |
| 2B.3 | Insert a card while no session is open | `status` says it is waiting. `station open` makes it join |
| 2B.4 | Pull a card while it uploads, put it in a different slot | `REMOVED`, then continues to `UPLOADED` with no duplicates |
| 2B.5 | Insert an ext4 or NTFS stick | It is not mounted, and a note says why |
| 2B.6 | `sudo systemctl restart sdu-daemon` during an upload | It comes back, settles interrupted files, and finishes |
| 2B.7 | Watch the Norelsys reader switch from bootloader (PID 9901) to active (PID 1081) | The card is not counted twice |

## Stage 2C: Boot and kiosk (Ubuntu)

| Step | Do this | Pass when |
| --- | --- | --- |
| 2C.1 | Reboot with the service enabled | `systemctl status sdu-daemon` is active, with a status line |
| 2C.2 | Pull the power during an upload, then switch on | The session continues; the bucket has no duplicate files |
| 2C.3 | `sudo kill -STOP $(pidof -s python3)` on the station (freeze it) for 6 minutes | The watchdog restarts it |
| 2C.4 | `sudo bash scripts/ubuntu/kiosk.sh enable`, reboot | The console fills the screen; Ctrl+Alt+F2 gives a login |
| 2C.5 | Close the console window in kiosk mode (Alt+F4) | It comes back within seconds |
| 2C.6 | `sudo bash scripts/ubuntu/kiosk.sh disable`, reboot | The normal desktop is back |

## Stage 3: Google Cloud

Use a test bucket or test prefix, and only data you are authorized to upload there.

| Step | Do this | Pass when |
| --- | --- | --- |
| 3.1 | `python main.py gcs-test` | `PASSED` |
| 3.2 | Run it with an identity that lacks write access | Clear permission error, nothing uploaded |
| 3.3 | Upload one file larger than 8 MiB, then compare with `gcloud storage hash` | CRC32C matches the report |

## Stage 4: Upload

| Step | Do this | Pass when |
| --- | --- | --- |
| 4.1 | Upload one card | `SUCCESS`, and the count in the bucket matches |
| 4.2 | Unplug Ethernet for 30 seconds during an upload | It retries and recovers, or ends with files `FAILED` that `retry` completes |
| 4.3 | Pull a card during an upload | Only that card's files fail. Others complete |
| 4.4 | Replug that card, possibly under a new drive letter, then `retry` | Finishes, with no duplicates |
| 4.5 | Close the terminal during an upload. Wait a minute, then `retry` | Finished files are not re-sent, session completes |
| 4.6 | `python main.py stop` from a second terminal | Stops after the files in progress. `retry` finishes |
| 4.7 | `verify --deep` with the cards attached | All match |
| 4.8 | Upload the same folder twice | Two separate session folders, nothing overwritten |

## Stage 5: Performance

| Step | Do this | Record |
| --- | --- | --- |
| 5.1 | `python main.py benchmark` with 1, 2, 5, 10, then all cards | Combined MB/s for each |
| 5.2 | Upload a sample with `--workers 4`, `8`, `12` | MB/s and failed attempts from each report |
| 5.3 | Try `chunk_size_mib` values of 16, 64 and 128 | MB/s, stability, memory use |
| 5.4 | Run the chosen settings at full load | CPU and RAM from Task Manager or `top` |

| Date | Cards | Hub / port | Workers | Chunk MiB | Read MB/s | Upload MB/s | Failed attempts | CPU / RAM |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |

## Before production

- [ ] Bucket IAM reviewed for least privilege
- [ ] Bucket confirmed private
- [ ] Data residency, retention and lifecycle agreed with the data owner
- [ ] Authorization confirmed for the recordings being uploaded
- [ ] Backup plan set for `data/database` and `data/reports`
- [ ] Operators have tried the workflow in the README
