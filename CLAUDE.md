# Contexto — Tatmon API / Reporte de Ventas
> Handoff actualizado tras sesión de Claude Code del 2026-07-18. La sesión anterior (claude.ai,
> 12-18 julio) dejó un diagnóstico parcial que esta sesión corrigió en varios puntos importantes
> — leer la sección "Lo que cambió respecto al handoff anterior" antes de asumir nada del texto viejo.

## ⚠️ Estado ahora mismo (2026-07-18, fin de sesión)
**3 de 5 tiendas están devolviendo 401 Unauthorized en `/tickets`: La Villa, Cayalá y
Quetzaltenango.** Kalú y Quiché responden bien. Probado repetidamente con
`/debug/tickets_pages?tienda=...`. Causa más probable: rate-limit temporal de MGR por el
volumen de pruebas de esta sesión (30-50+ llamadas en ~1 hora), que MGR señaliza como 401
en vez de 429. **Antes de seguir investigando, esperar un rato (30-60 min) y volver a
correr `/debug/tickets_pages?tienda=Tatmon%20La%20Villa&paginas=1` para confirmar si ya se
liberó.** Si el 401 persiste después de un cooldown largo, ahí sí revisar si la API key de
esas 3 tiendas expiró de verdad (poco probable que las 3 hayan expirado a la vez).

## Infraestructura activa
- **Railway:** `tatmon-api-production.up.railway.app` — auto-deploy desde `main` en GitHub, toma ~30-40s en reflejar un push.
- **Repo:** `github.com/hansastorga/tatmon-api` → código real en `tatmon-api/main.py` (está en el subdirectorio `tatmon-api/`, no en la raíz)
- **Cron:** GitHub Actions, `.github/workflows/reporte-diario.yml`, corre 03:00 UTC (9pm hora GT)
- **Secret del endpoint de envío:** `tatmon2026x7n`
- **Versión desplegada:** confirmado 2026-07-18 que Railway YA tiene `"version": "4.11"` corriendo (el `/` health check antes decía "4.9" pero eso quedó resuelto solo — probablemente alguien forzó un redeploy). **No hay nada pendiente aquí.**
- **5 tiendas:** LAVILLA, KALU, CAYALA, QUICHE, XELA — ver el bloqueador de 401 arriba antes de asumir que las 5 están sanas.

## Lo que cambió respecto al handoff anterior (2026-07-18)
El handoff anterior asumía que el bloqueador era "¿`invoice.number` coincide con el
número de recibo en `/payments`?". **Esa pregunta ya se resolvió y la respuesta es NO** —
pero el camino real de conciliación es otro, más caro de lo esperado. Ver detalle abajo.

### 1. El campo de conciliación real: `/ticketInvoices/{invoiceId}`
Confirmado con datos reales (no en la documentación de MGR, que no detalla el schema):
- `/tickets` trae `invoice.number` (ej. `20650`) e `invoice.id` (UUID).
- `/payments` NO trae ningún campo `number`/`ticket_id`/`invoice_id` — solo `id` (ej.
  `1192633`) y `payment_id` (ej. `1784316960`). **No hay cruce directo por ID entre
  `/tickets` y `/payments`.**
- Probar cruce por timestamp (`payment.date == invoice.last_payment_date`) falla en
  ~78% de los pagos — no es un método confiable (ver `/debug/reconciliacion`, la v1,
  descartada).
- **`GET /ticketInvoices/{invoiceId}`** (nunca antes probado — encontrado en
  docs.mygadgetrepairs.com/api/public-api/, no en el código) SÍ trae el detalle de la
  factura con un array `payments[]` embebido, y cada pago ahí tiene el mismo `id` y
  `payment_id` que aparece en `/payments`. **Este es el cruce real.**
- No existe un lookup inverso: `/payments/{id}` y `/ticketPayments/{id}` devuelven 404
  (probado). Hay que ir ticket → factura → pagos, no al revés.
- `/ticketInvoices/` (bulk, sin ID) trae `ticket.ticket_ref`, `amount_total`, `status`
  por factura pero **no** el array `payments[]` — ese solo sale en el detalle por ID.

### 2. Arquitectura propuesta para `get_dia_kpis()` v2 (implementada como prueba, NO activa aún)
Ver `reconciliar_pagos_tickets_v2()` y `fetch_invoice_details_for_tickets()` en
`main.py` (agregadas 2026-07-18, expuestas en `/debug/reconciliacion_v2`):
1. Traer tickets de una ventana de 60 días (confirmado con Hans — balance entre
   cobertura de cartera vieja y volumen de llamadas N+1).
2. Para cada ticket con invoice, llamar `/ticketInvoices/{invoice.id}` (threaded,
   8 workers) → construye índice `payment.id → ticket`.
3. Traer pagos reales del día (`/payments`, ya se hacía).
4. Clasificar cada pago real por el índice: si el ticket asociado fue creado hoy →
   venta del día; si fue creado antes → cartera recuperada; si no hay match → sin
   clasificar (mostrar, no esconder).
5. CxC = tickets creados hoy cuya factura (via el mismo índice) no tiene
   `status == "Paid"` — usa `amount_total` de la factura (real) en vez del regex de
   `parse_total()` sobre la descripción.

**Sin validar todavía si esto da los números correctos** — las corridas de prueba
contra el 16 de julio dieron resultados inconsistentes entre sí (ver bug de
confiabilidad abajo), y ahora mismo 3 tiendas están bloqueadas con 401, así que
cualquier número que se saque ahorita no es confiable. Repetir la validación cuando
el rate-limit se libere, antes de reemplazar `get_dia_kpis()` en producción.

### 3. Bug de confiabilidad encontrado y parcialmente arreglado
Los loops de paginación (`fetch_tickets_for_tienda_rango`, `fetch_payments_dia`,
`fetch_payments_dia_raw`, `fetch_pos_dia`) usaban `except Exception: break` genérico
— cualquier error transitorio (rate-limit, timeout, red) se trataba igual que "se
acabaron los datos", sin ningún aviso. Confirmado en vivo: la misma llamada a
`/debug/reconciliacion_v2?fecha=2026-07-16` dio 21 tickets/32 pagos una vez y 131
tickets/59 pagos la siguiente.

**Ya arreglado (2026-07-18):** se agregó `mgr_get()` (helper con 3 reintentos +
backoff) y se reemplazó en esas 4 funciones más `fetch_invoice_details_for_tickets`.
Los fallos persistentes (tras agotar reintentos) quedan en `FETCH_ERRORS`, visible en
`GET /debug/errores`.

**Pero la inestabilidad siguió apareciendo incluso con reintentos** — la explicación
más probable no es que los reintentos no alcancen, sino que estamos genuinamente
rate-limiteados por MGR ahora mismo (ver sección de estado arriba). Falta confirmar
esto con un cooldown antes de seguir. Si tras esperar sigue inconsistente, ahí sí
revisar si hay una suposición incorrecta sobre el orden de paginación de `/tickets`
(la lógica actual asume que viene ordenado por `created_date` descendente para saber
cuándo cortar — no confirmado a fondo, ver `/debug/tickets_pages`).

## Estructura actual del reporte (2 páginas reales, PDF vía ReportLab)
- **Pág 1:** KPIs + desglose de ingresos + tabla por sucursal + análisis
- **Pág 2:** desglose por categoría (Ventas POS / Reparaciones / Advances) + barra visual + detalle por sucursal

Nota: el handoff anterior decía "3 páginas, la 2 en blanco por bug de PageBreak" — el
código actual solo tiene UN `PageBreak()` (main.py, sección `generar_pdf_reporte`), o
sea 2 páginas en total, ambas con contenido. **No se ha confirmado con un PDF real
generado si efectivamente sale una página en blanco** — antes de tocar nada acá, correr
`/reporte/preview?fecha=YYYY-MM-DD` y mirar el PDF real, puede que este bug ya no exista
o que sea otra cosa (ej. overflow de contenido de página 1 a una página extra).

**Fuentes de datos actuales (sin cambiar todavía, get_dia_kpis sigue en su versión v1):**
- Revenue total → `/payments` (correcto, confirmado)
- Categorías venta/cartera → `classify_ticket()`/`fecha_pago_efectiva()` usando solo
  `invoice.last_payment_date` del ticket (defectuoso, ver arriba — reemplazo diseñado
  pero no activo)
- Ventas POS → `/posOrders` (correcto)

## Discrepancia original reportada por Tatmon (16 julio)
| Métrica | Reporte (esa fecha) | Real |
|---|---|---|
| Tickets | 26 | 32 |
| Venta del día | Q 15,069 | Q 11,820.20 |
| Cartera recuperada | Q 3,293 | Q 18,177 |
| Cuentas por cobrar | Q 0 | Q 2,099 |
| **Total cobrado** | ✅ Correcto | ✅ Correcto |

No se confirmó si esta tabla corresponde al 15 o al 16 de julio — al probar
`get_dia_kpis` para el 16, `tickets_total` daba 13, muy lejos de 26. Vale la pena
aclarar con Hans/el equipo la fecha exacta antes de usar esta tabla como referencia de
validación.

## Endpoints disponibles
### Producción
```
GET /                                          → health check
GET /kpis, /kpis/refresh, /tickets/all
GET /reporte/preview?fecha=YYYY-MM-DD
GET /reporte/enviar?fecha=YYYY-MM-DD&secret=tatmon2026x7n
```
### Debug / diagnóstico (agregados 2026-07-18, todos de solo lectura)
```
GET /debug/tiendas, /debug/payments?fecha=, /debug/pos?fecha=
GET /debug/invoice?tienda=&invoice_id=          → detalle de una factura (con payments[])
GET /debug/invoices_list?tienda=&page=          → bulk de facturas (sin payments[])
GET /debug/payment_detail?tienda=&payment_id=   → confirma que no hay lookup inverso (404)
GET /debug/reconciliacion?fecha=                → método v1 descartado (timestamp)
GET /debug/reconciliacion_v2?fecha=&ventana_dias=60  → método v2 (ticketInvoices), sin validar aún
GET /debug/errores                              → fallos persistentes tras reintentos (FETCH_ERRORS)
GET /debug/tickets_pages?tienda=&paginas=       → inspecciona orden crudo de paginación de /tickets
```

## Próximos pasos (en orden)
1. **Esperar cooldown y confirmar si el 401 de La Villa/Cayalá/Quetzaltenango se libera.**
   Sin esto, ningún número que salga de la API es confiable.
2. Re-correr `/debug/reconciliacion_v2?fecha=2026-07-16` un par de veces una vez
   liberado el rate-limit, confirmar que los resultados sean estables entre corridas.
3. Si sigue inestable con las 5 tiendas respondiendo bien, investigar el supuesto de
   orden de paginación en `fetch_tickets_for_tienda_rango` (línea con
   `if fechas and min(fechas) < desde_str: break`) — puede que MGR no garantice orden
   descendente estricto por `created_date`.
4. Con números estables, comparar `reconciliar_pagos_tickets_v2` contra la discrepancia
   real reportada (aclarando primero la fecha exacta con Hans/equipo).
5. Si los números cuadran, reemplazar `get_dia_kpis()` para usar esta lógica en
   producción (hoy solo existe como función de prueba en paralelo, no está conectada
   al reporte real).
6. Confirmar si el bug de "página en blanco" sigue existiendo generando un PDF real —
   el código ya no muestra evidencia clara de ese bug tal como está ahora.
7. Considerar aplicar `mgr_get()` (con reintentos) también a los endpoints `/debug/*`
   que todavía usan `requests.get` crudo, si se van a seguir usando para diagnóstico.

## Lógica de clasificación de tickets (ya implementada, no tocar)
- `issue_type = "Venta de equipos."` → Teléfono (anticipo/venta POS)
- Cualquier otro `issue_type` con valor → Reparación
- Sin `issue_type` → Excluir de categorización

**Nota importante:** Las ventas POS de teléfonos NO generan tickets en MGR — van
directo a caja. Solo generan ticket cuando hay anticipo previo. Por eso `/posOrders`
es la fuente correcta para ventas POS, no `/tickets`.

## Archivos relevantes en el repo
```
tatmon-api/main.py           → toda la lógica (~1050 líneas tras la sesión del 18 julio)
tatmon-api/Procfile
tatmon-api/railway.json
tatmon-api/requirements.txt
tatmon-api/logo.jpg           → logo usado en el PDF
.github/workflows/reporte-diario.yml
```

Funciones clave dentro de `main.py`:
- `mgr_get()` (~línea 40) — helper de reintentos con backoff, usar para cualquier
  llamada nueva a MGR en vez de `requests.get` directo
- `fetch_payments_dia()` / `fetch_payments_dia_raw()` — pagos del día (sumas vs crudo)
- `get_dia_kpis()` — arma el objeto de KPIs que consume el PDF actual — **sigue en su
  versión v1 (defectuosa), no reemplazada todavía**
- `fetch_pos_dia()` — ventas POS del día
- `classify_ticket()` / `fecha_pago_efectiva()` — clasificación actual (defectuosa) de venta/cartera, en producción todavía
- `reconciliar_pagos_tickets_v2()` / `fetch_invoice_details_for_tickets()` — lógica
  nueva propuesta, expuesta solo en `/debug/reconciliacion_v2`, no conectada al reporte
