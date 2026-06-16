# Discovery debugging

Use this when the hOn login succeeds but Home Assistant creates no Haier hOn
devices, or existing devices disappear after a reload.

## Enable integration debug logs

From Home Assistant Developer Tools -> Actions:

```yaml
action: haier_hon.set_log_level
data:
  level: debug
```

Then reload the Haier hOn integration, or restart Home Assistant if the reload
does not reproduce the issue.

To go back to normal logging:

```yaml
action: haier_hon.set_log_level
data:
  level: warning
```

The service changes runtime logger levels only; it is not persistent across a
Home Assistant restart.

## Optional: MQTT realtime logs

MQTT realtime is not required for device discovery. Polling still works without
MQTT, so do not enable MQTT debug unless you are specifically investigating
push updates.

```yaml
action: haier_hon.set_mqtt_log_level
data:
  level: debug
```

Return it to quiet mode after capture:

```yaml
action: haier_hon.set_mqtt_log_level
data:
  level: warning
```

## Persistent logger configuration

If you need debug from startup before services are registered, add this to
`configuration.yaml` temporarily:

```yaml
logger:
  default: warning
  logs:
    custom_components.haier_hon: debug
    custom_components.haier_hon._vendor.pyhon: debug
    pyhon: debug
    custom_components.haier_hon._vendor.pyhon.connection.mqtt: warning
    pyhon.connection.mqtt: warning
```

Restart Home Assistant, reproduce the issue, then remove the temporary block.

## What to look for

Successful setup should show this sequence:

```text
Setup debug: avvio setup ...
Connessione a hOn riuscita per ***@example.com
Setup debug: primo refresh coordinator in avvio
Trovati N dispositivi hOn
Caricati N dispositivi hOn con dati
Coordinator debug: aggiornamento dati hOn completato, dispositivi=N summary=[...]
Setup debug: forward piattaforme completato
```

Interpretation:

- `Trovati 0 dispositivi hOn` with no auth error means the hOn API accepted the
  login but returned an empty appliance list. The integration did not filter
  the devices; the cloud response was empty.
- `hOn API returned 0 appliances for this account (request OK)` means the same
  thing at the lower pyhOn API boundary.
- `invalid_auth`, `unauthorized`, `401`, `403`, `token`, or `session non
  disponibile` points to credentials, token refresh, or reauth.
- Setup reaching `Trovati N dispositivi hOn` but not `Caricati N dispositivi hOn
  con dati` means discovery worked, but one appliance update failed.
- `Lifecycle Connection Failure ... NOT_AUTHORIZED` from MQTT is only the
  optional realtime push channel. It does not explain missing devices when the
  polling lines above show devices.

## If the phone app shows devices but Home Assistant gets 0

Check these before changing code:

- Confirm the redacted email in the Home Assistant log is the same hOn account
  used in the phone app.
- In the phone app, verify the devices are owned by or explicitly shared with
  that same account.
- If Home Assistant uses a secondary/shared account, remove and recreate the
  share from the owner account.
- Log out and back into the phone app with the exact Home Assistant account; the
  app can keep a session for a different account than expected.
- Try the owner account once as a diagnostic comparison. If the owner account
  returns devices and the shared account returns 0, the issue is account/share
  state in hOn, not entity creation in Home Assistant.

