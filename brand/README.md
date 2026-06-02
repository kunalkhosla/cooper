# Cooper brand assets

| File | Use |
|---|---|
| `icon.svg` | Source mark (square). The repo logo and the basis for the Home Assistant brand icon. |
| `icon.png` / `icon@2x.png` | 256 / 512 px PNGs of the mark, ready for the Home Assistant brands repo. |
| `logo.svg` | Horizontal lockup (mark + `cooper` wordmark) for docs/headers. |

The mark: a bold geometric **C** in a sunrise gradient (gold → amber → coral) cradling a
glowing core, on a warm near-black tile. Warm but capable.

## Making the logo appear in Home Assistant

Home Assistant pulls integration logos from the **`home-assistant/brands`** repository, not
from this repo — so the icon shows up in HA only after a brands PR is merged. To submit:

1. Fork `https://github.com/home-assistant/brands`.
2. Add `custom_integrations/cooper/icon.png` (256×256) and `custom_integrations/cooper/icon@2x.png` (512×512) from this folder.
3. Open a PR. Once merged, HA shows the Cooper icon in Settings → Devices & Services and HACS.

Until then HA shows a default icon — the integration works regardless.
