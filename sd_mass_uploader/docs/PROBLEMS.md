# Problems and how to fix them

Generated from `src/advisor/catalog.py` by `python main.py problems --markdown`. Do not edit by hand.

Severity: **critical** stops all uploads; **error** needs a person; **warning** means it works but not as it should; **info** needs nothing.

## E-CONFIG: Station settings are incomplete

*critical*

The station cannot upload until its settings are fixed: details

1. Open the station's config.yaml (on an installed station: /etc/sd-mass-uploader/config.yaml) and correct the setting named above.
2. Restart the station service, or press Resume uploads.

Button: **Resume uploads**

## E-DATABASE: The station's local database failed

*critical*

The station could not read or write its own records. (details)

1. Check that the station computer's disk is not full.
2. Restart the station service.
3. If it continues, contact support with the technical details.

Button: **Resume uploads**

## E-GCS-AUTH: Google sign-in is not valid

*critical*

Uploads are paused for every card because Google Cloud rejected the station's credentials. Nothing was lost; files wait in the queue. (details)

1. On the station, renew the credentials: replace the key file at /etc/sd-mass-uploader/credentials.json, or run `gcloud auth application-default login`.
2. Press Resume uploads.
3. If it happens again at once, ask your Google Cloud admin whether the key was disabled.

Button: **Resume uploads**

## E-GCS-PERMISSION: No permission to write to the bucket

*critical*

Uploads are paused because the station's Google account may not write to the bucket. (details)

1. Ask your Google Cloud admin to grant the station's account `roles/storage.objectUser` (or create and get) on the bucket.
2. Press Resume uploads once they confirm.

Button: **Resume uploads**

## E-PEAKAI-KEY: PeakAI does not accept this laptop's key

*critical*

Uploads are paused: the PeakAI token service answered "HTTP 401 unknown laptop key". The key was switched off or is wrong. (details)

1. Ask PeakAI to check this laptop's key.
2. If they send a new pack, import it: python main.py pack import <zip>.
3. Then press Resume uploads.

Button: **Resume uploads**

## E-PEAKAI-NO-PASSES: This laptop has no PeakAI passes

*critical*

PeakAI has not given this laptop any upload pass, so nothing can be uploaded.

1. Ask PeakAI to add the passes this laptop needs.
2. The station checks again every 5 minutes by itself.

## E-SAFETY-BLOCK: A safety check blocked an action

*critical*

The station refused to run something that could change a card. No card was touched. (details)

1. Do not retry. Contact support with the technical details.

## E-SESSION-BUSY: Another program is uploading this session

*critical*

details. The station will not upload at the same time, to avoid duplicates.

1. Close the other upload (for example a `main.py upload` or `retry` in a terminal).
2. If it was a crash, wait one minute, then press Resume uploads.

Button: **Resume uploads**

## E-CARD-PARTLY-UNREADABLE: Some folders or files could not be read

*error*

Some item(s) on the card could not be read and were not uploaded. Example: details

1. Take the card out and put it back in, then press Retry to scan it again.
2. If the same items fail again, the card may be damaged. Keep it safe and report it.

Button: **Retry this card**

## E-CARD-REMOVED: Card removed before it finished

*error*

the card was taken out with Some file(s) not uploaded yet.

1. Put the same card back in, in any slot.
2. It continues by itself. Nothing is uploaded twice.

## E-CARD-UNREADABLE: Card could not be read

*error*

the card could not be scanned: details.

1. Take the card out, check the contacts, and put it back in.
2. Press Retry to scan it again.
3. If it fails again, set it aside. Do not repair or format it.

Button: **Retry this card**

## E-CONFLICT: A different file already exists in the bucket

*error*

Some file(s) from the card were not uploaded because a different file is already at the same place in the bucket. Nothing was overwritten. (details)

1. Ask your admin to compare the existing cloud file with this card.
2. To check again later: python main.py retry --session <ID> --include-conflicts.

## E-FILE-CHANGED: Files changed after the scan

*error*

Some file(s) on the card changed or disappeared after scanning, so they were not uploaded. (details)

1. Make sure no camera or computer is writing to the card.
2. End this session, then upload the card again in a new session so it is rescanned.

## E-FILE-READ: Files could not be read from the card

*error*

Some file(s) on the card could not be read. (details)

1. Take the card out and put it back in.
2. Press Retry.
3. If the same files fail again, the card may be damaged. Keep it safe and report it.

Button: **Retry this card**

## E-GCS-UNREACHABLE: Cannot reach Google Cloud

*error*

The station cannot connect to Google Cloud Storage, so uploads have not started. It tries again by itself. (details)

1. Check the Ethernet cable and that other websites open on this computer.
2. If the network is fine, check the Google Cloud status page.
3. Nothing is lost: cards stay queued and continue when the connection returns.

## E-INTERNAL: Unexpected error

*error*

Something went wrong that the station does not have specific advice for. (details)

1. Retry the card.
2. If it repeats, restart the station service and contact support with the details.

Button: **Retry this card**

## E-METADATA: Session details are not valid

*error*

The session could not be opened: details

1. Correct the session details and open the session again.

## E-MISSING-REMOTE: Files are missing from the bucket

*error*

Some file(s) from the card were uploaded earlier but are no longer in the bucket. (details)

1. Ask your admin whether a lifecycle or retention rule deletes objects.
2. Press Retry to upload them again.

Button: **Retry this card**

## E-MOUNT-FAILED: A card could not be opened

*error*

The station tried to open a card read-only and failed: details

1. Take the card out and put it back in.
2. Try another slot or reader.
3. Do not let the computer "repair" or format the card. Set it aside if it fails again.

## E-PEAKAI-INFO: The card's _info file was not written

*error*

the card is uploaded, but its _info file could not be written yet. The station tries again every minute. (details)

1. Check the internet connection.
2. Press Try again now.

Button: **Try again now**

## E-PEAKAI-PASS-DENIED: Storage refused this card's pass

*error*

Some file(s) from the card were refused (HTTP 403): the wrong pass for this card, or a pass this laptop does not have. (details)

1. Check which device recorded the card.
2. If the device is right, ask PeakAI to add that pass to this laptop.
3. Then press Retry.

Button: **Retry this card**

## E-PEAKAI-REPORT: The PeakAI dashboard was not updated

*error*

Nothing was recorded on the dashboard for this upload. The station tries again every minute. (details)

1. Check the internet connection.
2. If it says HTTP 401, ask PeakAI about this laptop's key.
3. Press Try again now.

Button: **Try again now**

## E-UPLOAD-FAILED: Upload kept failing

*error*

Some file(s) from the card ran out of retries because of network errors. (details)

1. Check the Ethernet cable and the internet connection.
2. Press Retry.

Button: **Retry this card**

## E-UPLOAD-REJECTED: Google Cloud rejected files

*error*

Google Cloud refused Some file(s) from the card in a way that retrying will not fix. (details)

1. Ask your admin to check bucket rules (retention, object size, location).
2. Press Retry once fixed.

Button: **Retry this card**

## E-VERIFY-MISMATCH: Cloud copy did not match the card

*error*

Some file(s) from the card arrived in the bucket different from the card. (details)

1. This usually means a network or hardware fault. Check the cable and hub.
2. To upload them again: python main.py retry --session <ID> --include-conflicts.

## W-CARD-CONFIRM: Confirm this device

*warning*

the card is held back: details.

1. Check that it is a recording card from this session, not a USB stick or a copy of another card.
2. Press Confirm to upload it, or remove it.

Button: **Confirm it is a card**

## W-CARD-DECISION: Which device recorded this card?

*warning*

the card is waiting: details.

1. Look at the card's folders and ask the person which device recorded it.
2. Choose that device, or skip the files that are not project footage.
3. Never guess a pass; if unsure, ask PeakAI.

Button: **Choose the device**

## W-CHECKSUM-UNAVAILABLE: Cloud checksum not available

*warning*

Google Cloud did not return a checksum for Some file(s) from the card, so they could not be fully verified.

1. Run python main.py verify --session <ID> later.

## W-DETECTION: Device detection had a problem

*warning*

details

1. Unplug and reconnect the hub or reader named above.
2. If it persists, restart the station service.

## W-DISK-LOW: The station's disk is almost full

*warning*

Only 2 GB is free where the station keeps its database and logs. When it is full, uploads stop.

1. Delete old reports you have backed up from the reports folder.
2. Free space elsewhere on this computer.

## W-FS-UNSUPPORTED: A device was not opened

*warning*

details. Only SD-card file systems are opened automatically.

1. If this is a camera card, check it in the camera; it may need formatting there (after its recordings are safe).
2. If it is a USB stick or another drive, remove it.

## W-INVALID-NAME: Some file names could not be read

*warning*

Some file(s) on the card have names this computer cannot read, so they were skipped. Example: details

1. Report these files; a copy with corrected names may be needed.

## W-NETWORK-RETRYING: Connection problems: retrying

*warning*

Some file(s) failed to send and are being retried (attempt 2 of 5). (details)

1. Check the Ethernet cable and the router.
2. Nothing is needed if it recovers; files that run out of retries can be retried later.

## W-PEAKAI-OLD-PASSES: PeakAI has not switched this laptop yet

*warning*

This laptop only has the old mono/stereo passes. The new client folders cannot be used until PeakAI switches it.

1. Tell the person, and ask PeakAI to switch this laptop to the new passes.
2. Cards wait; nothing is uploaded to the old folders by this station.

## W-PEAKAI-SERVICE: Cannot get upload passes right now

*warning*

The PeakAI token service did not answer. The station keeps the current tokens and tries again every minute. (details)

1. If it says HTTP 000: fix the internet connection. It picks up by itself.
2. If it says HTTP 502: nothing to do; Google did not answer just then.
3. If it lasts more than 15 minutes, tell PeakAI.

## W-SIMULATOR-MISSING: Simulator folder not found

*warning*

details

1. Create the folder, or correct station.simulator_dir.

## W-UNCERTAIN: Interrupted files need their card

*warning*

Some file(s) from the card were interrupted. The station needs the card to check whether the cloud copies are complete.

1. Insert the card. The check runs by itself.

## W-UNSAFE-NAME: Some file names cannot be used in the bucket

*warning*

Some file(s) on the card were skipped because their names cannot be stored safely. Example: details

1. Report these files; they need to be renamed on a copy before upload.

## W-USB-SLOW: Card reader on a slow USB connection

*warning*

the card is connected at 480 Mbps. It works, but reads much slower than USB 3 (5 Gbps).

1. Move the reader to a blue USB 3 port on the hub or laptop.
2. Use a USB 3 cable and hub; some hub ports are USB 2 only.

## I-PEAKAI-DETAILS: Recording dates or hours are not known

*info*

The station could not read them from the cards (details). The dashboard entry is sent without them.

1. Ask the person for the first and last recording day and the total video hours.
2. Add them to the session details (recorded_from, recorded_to, video_hours).

## I-SKIPPED: Some items were skipped

*info*

Some link(s) or special file(s) on the card were not uploaded. This is normal for camera cards. Example: details

1. Nothing to do unless these items are expected recordings.

## I-STOPPED: Files were stopped

*info*

Some file(s) from the card were stopped part-way and went back in the queue.

1. Nothing to do.

