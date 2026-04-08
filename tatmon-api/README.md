# Tatmon Producción API

Servicio intermedio entre MyGadgetRepairs y el dashboard de producción de Tatmon.

## Endpoints

| Endpoint | Descripción |
|---|---|
| `GET /` | Health check |
| `GET /kpis` | KPIs calculados de toda la red (cache 1 hora) |
| `GET /kpis/refresh` | Fuerza recalculo inmediato |
| `GET /tickets/raw` | Primeros 10 tickets en crudo (debug de campos) |

## Deploy en Railway

1. Crear cuenta en railway.app
2. New Project → Deploy from GitHub repo
3. Agregar variable de entorno: `MGR_API_KEY` = tu API key de MGR
4. Railway detecta Python automáticamente y despliega

## Variables de entorno requeridas

```
MGR_API_KEY=tu_api_key_de_mgr
```
