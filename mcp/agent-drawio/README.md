# openbrain-drawio (QJM)

Lokaler MCP-Server fuer draw.io-Diagramme. Ersetzt den frueheren Google-Drive-Server
aus openBrain. Diagramme werden als .drawio-Dateien im QJM-dsh_playground abgelegt.

## Tools

- create_drawio_diagram(title, nodes, edges, filename?, overwrite?, return_xml?)
  Erzeugt ein Diagramm und schreibt es als .drawio-Datei.
- list_drawio_diagrams()
  Listet alle lokalen .drawio-Dateien (Name, Pfad, Groesse, Aenderungszeit).
- read_drawio_diagram(filename)
  Liest eine .drawio-Datei ueber ihren Dateinamen.

## Speicherort

- Host:      /home/daniel/QJM/dsh_playground/drawio
- Container: /dsh_playground/drawio   (DRAWIO_BASE_DIR)

Kein Google Drive, keine Google-Credentials.

## Umgebungsvariablen

- DRAWIO_BASE_DIR  Ablageordner (Default /dsh_playground/drawio)
- PORT             HTTP-Port (Default 8796)
- MCP_ACCESS_KEY   Pflicht fuer HTTP; ohne Key werden Anfragen mit 401 abgelehnt

## Lokal starten

    /home/daniel/.deno/bin/deno check index.ts stdio.ts tools/*.ts
    /home/daniel/.deno/bin/deno run -A index.ts

## Tests

    bash tests/call.sh tools/list
    bash tests/e2e.sh
