# pleno-recognizers

Pure-Python recognizer definitions (regex plus checksum validators) for Japanese PII. Shared by the [pleno-anonymize](https://github.com/plenoai/pleno-anonymize) HTTP server and the `pleno-pii-scanner` CLI so identical input yields identical entity sets across both surfaces.

## Install

```sh
uv add pleno-recognizers
# Presidio adapter (optional)
uv add 'pleno-recognizers[presidio]'
```

## Entities

`PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` `BANK_ACCOUNT`

## License

AGPL-3.0-or-later
