"""Plantilla del vertical "discoteca".

Es lo que convierte el alta de un boliche nuevo en minutos y no en una tarde de
carga de datos (ANALISIS.md seccion 11 quater). Los numeros son de referencia y
el local los ajusta despues.
"""

from __future__ import annotations

from decimal import Decimal

UNIDADES = [
    ("ml", "Mililitro", "volumen", "1"),
    ("l", "Litro", "volumen", "1000"),
    ("g", "Gramo", "masa", "1"),
    ("kg", "Kilogramo", "masa", "1000"),
    ("u", "Unidad", "unidad", "1"),
]

CATEGORIAS = [
    ("Cervezas", 10),
    ("Destilados", 20),
    ("Tragos", 30),
    ("Sin alcohol", 40),
    ("Mixers", 50),
]

# (nombre, unidad_base, costo_promedio, stock_minimo, presentacion, factor)
INSUMOS = [
    ("Ron", "ml", "0.90", "1500", "Botella 750 ml", "750"),
    ("Vodka", "ml", "0.85", "1500", "Botella 750 ml", "750"),
    ("Gin", "ml", "1.00", "1500", "Botella 750 ml", "750"),
    ("Whisky", "ml", "1.60", "750", "Botella 750 ml", "750"),
    ("Fernet", "ml", "1.10", "750", "Botella 750 ml", "750"),
    ("Tequila", "ml", "1.30", "750", "Botella 750 ml", "750"),
    ("Cerveza barril", "ml", "0.12", "30000", "Barril 50 l", "50000"),
    ("Cerveza lata", "u", "45.00", "48", "Caja de 24", "24"),
    ("Cola", "ml", "0.09", "6000", "Botella 2 l", "2000"),
    ("Tonica", "ml", "0.11", "4000", "Botella 1.5 l", "1500"),
    ("Agua", "u", "30.00", "24", "Caja de 12", "12"),
    ("Hielo", "g", "0.02", "10000", "Bolsa 2 kg", "2000"),
    ("Vaso descartable", "u", "4.50", "200", "Paquete de 50", "50"),
]

# (categoria, nombre, precio, [(insumo, cantidad, unidad)])
PRODUCTOS = [
    ("Tragos", "Cuba libre", "350", [("Ron", "50", "ml"), ("Cola", "150", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Tragos", "Gin tonic", "380", [("Gin", "50", "ml"), ("Tonica", "150", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Tragos", "Vodka tonic", "350", [("Vodka", "50", "ml"), ("Tonica", "150", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Tragos", "Fernet con cola", "350", [("Fernet", "50", "ml"), ("Cola", "150", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Destilados", "Whisky", "450", [("Whisky", "50", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Destilados", "Tequila", "400", [("Tequila", "50", "ml"), ("Hielo", "150", "g"), ("Vaso descartable", "1", "u")]),
    ("Cervezas", "Chopp", "220", [("Cerveza barril", "500", "ml"), ("Vaso descartable", "1", "u")]),
    ("Cervezas", "Cerveza lata", "180", [("Cerveza lata", "1", "u")]),
    ("Sin alcohol", "Agua", "120", [("Agua", "1", "u")]),
]

# (nombre, precio, [(producto hijo, cantidad)])
COMBOS = [
    ("Chopp + Tequila", "560", [("Chopp", 1), ("Tequila", 1)]),
]
