"""
warehouse.clases
================

Vistas con el "apellido" de cada instrumento: la clase de activo y el detalle
que la mesa usa para hablar.

El anexo no trae esto. Trae codigos (`BTU`, `BEE`, `ICP`) que remiten a una
tabla de codificacion que vive fuera del PDF, en el SEIL. Asi que la
clasificacion se deriva de la evidencia que si esta en los datos, y cada
regla deja escrito de donde sale.

Anclas usadas
-------------
* RUT 60805000 emite todos los BTU y BTP, con nemotecnicos BTU0210750 y
  BTP0470930: Tesoreria General de la Republica. Son soberanos.
* RUT 60801000 emite los BEC: Banco Central. Soberano tambien.
* El tipo BEBCE del anexo extranjero tiene 36 emisores y el mas frecuente es
  US TREASURY N/B. Soberano extranjero.
* BEE tiene 863 emisores distintos y BBFE 294, encabezados por corporativos y
  bancos respectivamente.

PROM no es una moneda
---------------------
El 91% de los CCS informa `MONEDA_POSICION_CORTA = 'PROM'`, que no es un
codigo de moneda sino una convencion de precio. Que representa al dolar se
confirmo cruzando el libro de BBVA NY contra lo que Confuturo informa a la
CMF: las mismas operaciones que BBVA registra como `CLF/USD` aparecen del
lado CMF como `UF/PROM`, y las que BBVA registra como `EUR/CLF` aparecen como
`UF/EUR`. Por eso PROM se normaliza a USD, y queda anotado que es una
inferencia contrastada, no una lectura directa del campo.

Lo que no se puede clasificar queda en un balde explicito -- `SIN_CLASIFICAR`
y `OTROS` -- en vez de forzarse a una categoria. Un instrumento mal
etiquetado en un grafico de curva es peor que uno que no aparece.
"""

from __future__ import annotations

#: Normalizacion de unidad de denominacion. PROM -> USD por el contraste con
#: el libro de BBVA descrito arriba; el resto es identidad.
_MONEDA = """
    CASE upper(trim({col}))
        WHEN 'PROM' THEN 'USD'
        WHEN '$$'   THEN 'CLP'
        WHEN ''     THEN NULL
        ELSE upper(trim({col}))
    END
"""

#: Indice flotante de un swap: la pata que NO es fija le da el apellido.
#: Los valores observados son ICP, CLP TNA, LIBO, LIBOR_, SOFR M, SOFRC,
#: CPTFEMU INDEX y VAR.
_INDICE = """
    CASE
        WHEN {a} IS NULL AND {b} IS NULL              THEN 'SIN_DETERMINAR'
        WHEN upper({a}) LIKE 'ICP%'  OR upper({b}) LIKE 'ICP%'  THEN 'Camara'
        WHEN upper({a}) LIKE 'TAB%'  OR upper({b}) LIKE 'TAB%'  THEN 'TAB'
        WHEN upper({a}) LIKE 'SOFR%' OR upper({b}) LIKE 'SOFR%' THEN 'SOFR'
        WHEN upper({a}) LIKE 'LIBO%' OR upper({b}) LIKE 'LIBO%' THEN 'LIBOR'
        WHEN upper({a}) LIKE 'CLP TNA%' OR upper({b}) LIKE 'CLP TNA%' THEN 'CLP TNA'
        WHEN upper({a}) LIKE 'CPTFEMU%' OR upper({b}) LIKE 'CPTFEMU%' THEN 'Euribor/CPTFEMU'
        WHEN upper({a}) = 'VAR'      OR upper({b}) = 'VAR'      THEN 'Variable s/d'
        WHEN upper({a}) = 'FIJA' AND upper({b}) = 'FIJA'        THEN 'Fija contra Fija'
        ELSE 'SIN_DETERMINAR'
    END
"""

SQL_CLASES = f"""
-- ===========================================================================
--  v_derivado_clasificado: cada operacion con su clase de activo y apellido
-- ===========================================================================
CREATE OR REPLACE VIEW v_derivado_clasificado AS
WITH base AS (
    SELECT d.*,
           {_MONEDA.format(col='d.moneda_larga')}      AS m_larga,
           {_MONEDA.format(col='d.moneda_corta_swap')} AS m_corta,
           {_MONEDA.format(col='d.moneda')}            AS m_fwd,
           {_INDICE.format(a='d.pata_larga_tipo', b='d.pata_corta_tipo')} AS indice
    FROM fact_derivado d
)
SELECT *,
    -- --- clase de activo de primer nivel --------------------------------
    CASE
        WHEN subtipo = 'IRS'                                   THEN 'TASAS'
        WHEN subtipo = 'CCS'                                   THEN 'FX / CCS'
        WHEN producto = 'FORWARD'
             AND subyacente_contrato = 'TASA_O_INFLACION'      THEN 'TASAS'
        WHEN producto = 'FORWARD'                              THEN 'FX / CCS'
        WHEN producto = 'PACTO'                                THEN 'FINANCIAMIENTO'
        WHEN producto IN ('OPCION', 'FUTURO')                  THEN 'OTROS DERIVADOS'
        ELSE 'OTROS'
    END AS clase_activo,

    -- --- el apellido -----------------------------------------------------
    CASE
        -- IRS: lo nombra su indice flotante
        WHEN subtipo = 'IRS' AND indice = 'SIN_DETERMINAR' THEN 'IRS (indice s/d)'
        WHEN subtipo = 'IRS' AND indice = 'Fija contra Fija' THEN 'IRS Fija-Fija'
        WHEN subtipo = 'IRS' THEN 'IRS ' || indice

        -- CCS: lo nombra el cruce de monedas
        WHEN subtipo = 'CCS' AND m_larga IS NOT NULL AND m_corta IS NOT NULL
             THEN 'CCS ' || m_larga || '/' || m_corta
        WHEN subtipo = 'CCS' THEN 'CCS (cruce s/d)'

        -- Forward de inflacion contra forward de moneda
        WHEN producto = 'FORWARD' AND subyacente_contrato = 'TASA_O_INFLACION'
             THEN 'Forward UF'
        WHEN producto = 'FORWARD' AND m_fwd IS NOT NULL
             THEN 'Forward FX ' || m_fwd
        WHEN producto = 'FORWARD' THEN 'Forward FX (moneda s/d)'

        WHEN producto = 'PACTO'  THEN 'Pacto'
        WHEN producto = 'OPCION' THEN 'Opcion'
        WHEN producto = 'FUTURO' THEN 'Futuro'
        ELSE 'SIN_CLASIFICAR'
    END AS instrumento,

    indice                                   AS indice_flotante,
    m_larga                                  AS moneda_recibe,
    m_corta                                  AS moneda_entrega,
    CASE WHEN m_larga IS NOT NULL AND m_corta IS NOT NULL AND m_larga <> m_corta
         THEN m_larga || '/' || m_corta END  AS cruce_monedas,

    -- Tenor en anios al cierre del periodo: es el eje X de la curva.
    DATE_DIFF('day',
        LAST_DAY(STRPTIME(CAST(periodo_informacion AS VARCHAR) || '01', '%Y%m%d')),
        fecha_vencimiento) / 365.25          AS tenor_anios,

    -- Direccionalidad legible para colorear el scanner.
    CASE rol_tasa_fija
        WHEN 'PAGA_FIJA'                THEN 'Paga Fija'
        WHEN 'RECIBE_FIJA'              THEN 'Recibe Fija'
        WHEN 'FIJA_CONTRA_FIJA'         THEN 'Fija contra Fija'
        WHEN 'FLOTANTE_CONTRA_FLOTANTE' THEN 'Flotante contra Flotante'
        ELSE 'Sin determinar'
    END AS direccion,

    -- La tasa fija del contrato: la que va al eje Y de la curva. Si una pata
    -- es fija se usa esa; si las dos lo son, la larga.
    CASE
        WHEN upper(COALESCE(pata_larga_tipo, '')) = 'FIJA' THEN pata_larga_tasa
        WHEN upper(COALESCE(pata_corta_tipo, '')) = 'FIJA' THEN pata_corta_tasa
        ELSE tasa_precio_contrato
    END AS tasa_fija,

    COALESCE(mtm_contrato_m,
             COALESCE(mtm_activo_m, 0) - COALESCE(mtm_pasivo_m, 0)) AS mtm_neto_m
FROM base;


-- ===========================================================================
--  v_renta_fija_clasificada: local y extranjera en una sola vista,
--  segmentada por emisor soberano / bancario / corporativo
-- ===========================================================================
CREATE OR REPLACE VIEW v_renta_fija_clasificada AS
SELECT
    periodo_informacion, rut_compania, zip_origen, source_file, line_no,
    'LOCAL'                                   AS ambito,
    nemotecnico                               AS instrumento_id,
    tipo_instrumento,
    CAST(emisor_rut AS BIGINT)                AS emisor_rut,
    NULL                                      AS emisor_nombre,
    {_MONEDA.format(col='unidad_monetaria')}  AS moneda,
    valor_final, tir_compra, tir_mercado, tasa_emision,
    duracion_modificada_aprox                 AS duracion,
    'aproximada'                              AS duracion_origen,
    fecha_vencimiento, clasificacion_riesgo, clasificacion_inversion,
    NULL                                      AS en_margen_o_pacto,
    -- Tesoreria (60805000) y Banco Central (60801000) emiten todos los BTU,
    -- BTP y BEC. Los codigos bancarios e hipotecarios salen del propio
    -- tipo_instrumento. Lo que no calza queda en Otros, sin forzar.
    CASE
        WHEN emisor_rut IN (60805000, 60801000)        THEN 'Soberano'
        WHEN tipo_instrumento IN ('BTU','BTP','BEC')   THEN 'Soberano'
        WHEN tipo_instrumento IN ('BR')                THEN 'Soberano (reconocimiento)'
        WHEN tipo_instrumento IN ('BB','BU','LH','DPB','PDBC') THEN 'Bancario'
        WHEN tipo_instrumento IN ('MHA','MHB','CLEAS','MHE')   THEN 'Hipotecario / Leasing'
        WHEN tipo_instrumento IN ('BE','BS','BEF','BVL','BNEE','BTU')  THEN 'Corporativo'
        ELSE 'Otros'
    END AS segmento_emisor
FROM fact_renta_fija

UNION ALL BY NAME

SELECT
    periodo_informacion, rut_compania, zip_origen, source_file, line_no,
    'EXTRANJERO'                              AS ambito,
    isin                                      AS instrumento_id,
    tipo_instrumento,
    NULL                                      AS emisor_rut,
    emisor                                    AS emisor_nombre,
    {_MONEDA.format(col='moneda')}            AS moneda,
    valor_final, tir_compra, tir_mercado, tasa_emision,
    duracion                                  AS duracion,
    'informada'                               AS duracion_origen,
    fecha_vencimiento, clasificacion_riesgo, clasificacion_inversion,
    en_margen_o_pacto,
    -- BEBCE son bonos de estado y banco central extranjeros: el emisor mas
    -- frecuente es US TREASURY N/B. BBFE y DPBFE son bancarios. BEE y BEEC,
    -- corporativos.
    CASE
        WHEN tipo_instrumento IN ('BEBCE')          THEN 'Soberano'
        WHEN tipo_instrumento IN ('BBFE','DPBFE')   THEN 'Bancario'
        WHEN tipo_instrumento IN ('BEE','BEEC')     THEN 'Corporativo'
        ELSE 'Otros'
    END AS segmento_emisor
FROM fact_extranjero_rf;
"""
