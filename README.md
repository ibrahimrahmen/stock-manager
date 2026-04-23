# Stock Manager — Django

Système de gestion de stock par unité individuelle avec scan de codes-barres.

## Installation

```bash
cd stock_manager
pip install -r requirements.txt
python manage.py makemigrations inventory
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## URLs principales

| URL | Description |
|-----|-------------|
| `/` | Dashboard |
| `/scan/shipping/` | Scan expédition (bordereaux + unités) |
| `/scan/reception/` | Scan réception (ajouter stock) |
| `/admin/` | Back-office Django |

## Format des barcodes

```
RLF-RED-S-001
│   │   │  └── Numéro séquentiel (001, 002, ...)
│   │   └───── Taille (XS, S, M, L, XL, XXL, UNIQUE)
│   └───────── Couleur (doit correspondre à color_name dans ProductVariant)
└───────────── Code produit (doit correspondre à code dans Product)
```

## Workflow de réception

1. Créer le **Product** dans l'admin (ex: nom="Ralph", code="RLF")
2. Créer la **ProductVariant** (ex: color_name="RED", color_label="Rouge", + uploader la photo)
3. Aller sur `/scan/reception/` et scanner les barcodes des unités

## Workflow d'expédition

1. Aller sur `/scan/shipping/`
2. Scanner le **bordereau** de la compagnie de livraison → ouvre un ordre
3. Scanner les **unités** à expédier une par une
4. Scanner le **bordereau suivant** → ferme l'ordre courant + ouvre le nouveau

## Statuts d'une ProductUnit

```
in_stock → in_order → shipped
              ↓
           returned → in_stock
```
