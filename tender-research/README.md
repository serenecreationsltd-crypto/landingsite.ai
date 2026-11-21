# Uganda PPDA tender research archive

The Public Procurement and Disposal of Public Assets Authority of Uganda publishes the source data through its [Government Procurement Portal](https://gpp.ppda.go.ug/public/open-data/ocds/ocds-datasets). Open Contracting Partnership makes the compiled releases available in its [Uganda PPDA Data Registry entry](https://data.open-contracting.org/en/publication/130). The source database is licensed under the [Open Data Commons Attribution License](https://opendatacommons.org/licenses/by/1-0/).

Serene Creations extracts records with a tender title, keeps one row per contracting process identifier, and groups rows by source export year. The compressed indexes omit supplier details and standalone awards and contracts. The archive records the source's status and dates. Use the issuer's current notice for bid eligibility and deadlines.

Run `python tender-research/build_ppda_archive.py --snapshot YYYY-MM-DD 2024 2025 2026` to regenerate the indexes. Use the source snapshot date listed in the registry. A build writes content-addressed year files and then updates the manifest. Commit the new year files and manifest together.
