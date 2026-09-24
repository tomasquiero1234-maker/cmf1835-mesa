"""
reportes
========

Exportacion estatica a Excel del stock de inversiones de las aseguradoras.

Es un modulo PARALELO al dashboard web: lee el warehouse en solo-lectura, no
importa nada de app/ y no modifica ninguna vista. Lo que el Excel corrige
respecto del dashboard (publicacion vigente en todas las tablas, entidades por
identificador legal) lo corrige en sus propias consultas.

    python -m reportes --periodo 202608
"""
