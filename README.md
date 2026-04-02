# M3 Export Web App

Web app Flask per Oracle Linux 8.5 che:

- legge i `target` da `m3data.aggregation_index`
- mostra le `resource` associate al target scelto
- filtra per `data inizio` e `data fine`
- usa `table_uuid` per leggere i dati nello schema `aggregatedData`
- esporta Excel con prima colonna `Data`
- usa `avg` se `crescente = 0`
- usa `delta` se `crescente = 1`
- intestazioni Excel nel formato `target/resource(um)`

## Struttura directory

```bash
/opt/m3_export_webapp/
├── app/
│   ├── app.py
│   └── templates/
│       └── index.html
├── .env
├── requirements.txt
├── run.sh
└── m3-export.service
```

## Creazione directory sul server

```bash
mkdir -p /opt/m3_export_webapp/app/templates
```

## Pacchetti di sistema consigliati

```bash
dnf install -y python39 python39-pip
```

## Installazione

```bash
cd /opt
unzip m3_export_webapp.zip -d /opt
mv /opt/m3_export_webapp /opt/m3_export_webapp_tmp 2>/dev/null || true
mv /opt/m3_export_webapp_tmp /opt/m3_export_webapp 2>/dev/null || true
cd /opt/m3_export_webapp
python3.9 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
vi .env
chmod +x run.sh
cp m3-export.service /etc/systemd/system/m3-export.service
systemctl daemon-reload
systemctl enable --now m3-export.service
```

## Avvio manuale per test

```bash
cd /opt/m3_export_webapp
python3.9 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app/app.py
```

## URL

```bash
http://IP_SERVER:8080/
```

## Note tecniche

- La query su `aggregatedData."table_uuid"` è dinamica e usa il `table_uuid` preso da `m3data.aggregation_index`.
- Il merge tra serie diverse viene fatto su `ts` tramite outer join, così nell'Excel restano tutte le date presenti.
- La conversione data assume che `ts` sia in epoch secondi.
- Se nel tuo DB `ts` fosse in millisecondi, in `app.py` va cambiato `unit="s"` in `unit="ms"` e i filtri data devono essere convertiti di conseguenza.

## Query logica usata

### Metadati

```sql
SELECT target, resource, COALESCE(um, '') AS um, table_uuid, crescente
FROM m3data.aggregation_index
WHERE disabled = 0
  AND target = :target
  AND resource = ANY(:resources)
  AND table_uuid IS NOT NULL
ORDER BY resource;
```

### Dati serie

```sql
SELECT ts, avg AS value
FROM aggregatedData."UUID"
WHERE ts >= :from_epoch AND ts <= :to_epoch
ORDER BY ts;
```

oppure

```sql
SELECT ts, delta AS value
FROM aggregatedData."UUID"
WHERE ts >= :from_epoch AND ts <= :to_epoch
ORDER BY ts;
```


## Compatibilità Python

Questa versione è compatibile con **Python 3.6**.
Se sul server hai solo Python 3.6, usa questo pacchetto aggiornato.



###### Per GIT

# m3_export_webapp
