# `report/` – Excel output

`excel.py` builds the workbook with `openpyxl`:

| Sheet | Content |
|---|---|
| **Arbitrage** | Pairs with a positive net edge after fees, `OK` rows first, best return on capital first |
| **All matches** | Every pair the matcher accepted (arbitrage or not) |
| **Not matched (near misses)** | Closest rejected pairs and why (including `guard: …` rejections) |
| **Unpaired events** | Multi-choice / ladder events with no counterpart on the other platform |
| **Notes** | Column explanations and caveats |

The report is written by `cli.py`; the output folder is `OUTPUT_DIR` in `../config.py`.
