# Calendario Académico · Maqueta desplegable

Maqueta web construida con **Streamlit + SQLite**, preparada para ejecutarse localmente o publicarse en **Streamlit Community Cloud**.

## Accesos de demostración

- Administrador: `admin@demo.cl` / `Admin123!`
- Líder: `lider@demo.cl` / `Lider123!`
- Visualizador: `visual@demo.cl` / `Visual123!`

> Son credenciales de demostración. No usar datos sensibles ni reales mientras se mantengan estas claves.

## Ejecutar localmente

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
streamlit run app.py
```

Abrir `http://localhost:8501`.

## Despliegue

Ver **GUIA_DESPLIEGUE.md**. El repositorio está preparado para subirlo tal cual a GitHub y seleccionar `app.py` como archivo principal en Streamlit Community Cloud.

## Persistencia

SQLite permite probar toda la lógica sin contratar una base de datos. En Streamlit Community Cloud el almacenamiento local **no es persistente garantizado**: un reinicio o redeploy puede recrear la base con los datos iniciales de `seed_data.json`.

Para una prueba institucional prolongada o producción, migrar la capa de datos a PostgreSQL/Supabase/Azure SQL.

## Correo

Las notificaciones internas funcionan sin configuración. El correo es opcional. En Streamlit Cloud, agregar en **App settings > Secrets** los valores indicados en `.streamlit/secrets.example.toml`.
