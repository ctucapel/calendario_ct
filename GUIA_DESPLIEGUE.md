# Guía de despliegue · Streamlit Community Cloud

## Opción recomendada para la maqueta

**GitHub + Streamlit Community Cloud**. Para esta etapa no necesitas servidor propio.

### Paso 1 · Crear una cuenta de GitHub

1. Entra a https://github.com/ y crea una cuenta si no tienes una.
2. Verifica tu correo.

### Paso 2 · Crear el repositorio

1. En GitHub selecciona **New repository**.
2. Nombre sugerido: `calendario-academico`.
3. Para una maqueta sin información real puede ser público. Si utilizarás nombres/correos reales, usa **Private**.
4. No marques opciones para crear README, .gitignore o licencia: este proyecto ya los incluye.
5. Crea el repositorio.

### Paso 3 · Subir este proyecto

La forma más fácil, sin Git:

1. Abre el repositorio recién creado.
2. Selecciona **uploading an existing file** / **Add file > Upload files**.
3. Arrastra **el contenido de esta carpeta**, no el ZIP.
4. Deben verse en la raíz al menos:
   - `app.py`
   - `db.py`
   - `export_utils.py`
   - `seed_data.json`
   - `requirements.txt`
   - `README.md`
   - `GUIA_DESPLIEGUE.md`
   - carpeta `.streamlit` (si GitHub Web no permite cargarla fácilmente, no es obligatoria para arrancar; solo contiene configuración visual).
5. Confirma con **Commit changes**.

### Paso 4 · Conectar GitHub con Streamlit

1. Entra a https://share.streamlit.io/.
2. Inicia sesión y conecta tu cuenta de GitHub.
3. Autoriza a Streamlit para acceder al repositorio elegido.

### Paso 5 · Crear la aplicación

1. En Streamlit Community Cloud selecciona **Create app**.
2. Indica que ya tienes una aplicación.
3. Selecciona:
   - **Repository:** `<tu_usuario>/calendario-academico`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. En **App URL** puedes intentar, por ejemplo: `calendario-academico-duoc`.
5. Pulsa **Deploy**.

Cuando termine, tendrás una dirección del tipo:

`https://calendario-academico-duoc.streamlit.app`

### Paso 6 · Probar el flujo

Ingresa primero como Administrador:

- `admin@demo.cl`
- `Admin123!`

Luego abre una ventana de incógnito u otro navegador e ingresa como Líder:

- `lider@demo.cl`
- `Lider123!`

Con dos sesiones puedes probar la lógica multiusuario.

### Paso 7 · Habilitar correo, si lo deseas

No es necesario para la primera prueba. Para activarlo:

1. Abre la aplicación en Streamlit Community Cloud.
2. Entra a **App settings > Secrets**.
3. Copia los parámetros de `.streamlit/secrets.example.toml` y reemplaza los valores por los del servidor SMTP autorizado.
4. Reinicia la aplicación.

Nunca subas contraseñas de correo a GitHub.

## Importante sobre SQLite

Esta maqueta usa `calendar.db`, que se crea automáticamente. En Community Cloud funciona para demostración, pero los datos modificados pueden perderse después de un reinicio, actualización del código o reconstrucción de la aplicación. El calendario inicial volverá a crearse desde `seed_data.json`.

Para una prueba con datos reales y persistencia, la siguiente etapa debe mover la base a PostgreSQL/Supabase u otra base administrada.

## Actualizar la aplicación

Cuando cambies archivos en GitHub, Streamlit Community Cloud detectará los cambios y volverá a desplegar la aplicación automáticamente.

## Errores frecuentes

- **ModuleNotFoundError:** revisar que `requirements.txt` esté en la raíz.
- **No encuentra app.py:** comprobar que el Main file path sea exactamente `app.py`.
- **La aplicación vuelve a los datos demo:** es el comportamiento esperado si el contenedor de SQLite se reinició.
- **Correo no funciona:** la aplicación sigue funcionando; revisar Secrets y credenciales SMTP.
