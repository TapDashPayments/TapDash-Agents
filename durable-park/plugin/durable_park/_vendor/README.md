# Vendored dependencies (do not edit)

| Package | Version | Source sdist sha256 | License |
|---|---|---|---|
| fastjsonschema | 2.22.2 | `72064e12356a7d6ef02165be2946b9abadbdf238536e07eb587e3dbaa33099cf` | BSD-3-Clause (`LICENSE.fastjsonschema`) |

Copied unchanged from the PyPI sdist `fastjsonschema-2.22.2.tar.gz` (package directory only;
`__main__.py` removed). Imported as `durable_park._vendor.fastjsonschema` so it never clashes with a
copy in Hermes's own environment.

Update: `pip download --no-deps --no-binary :all: fastjsonschema==<v>`, check the sha256 against PyPI,
replace the `fastjsonschema/` directory, update this table, run the full test suite.
