# Actualización funcional V2

Esta actualización incorpora:

- PDF con los tres formatos originales del Excel, una página por formato:
  1. CA Procesos (vertical)
  2. CA Cronológico (vertical)
  3. CA Gráfico 2027 (horizontal)
- Los tres Excel de descarga usan la plantilla original y conservan formato, colores, bordes, logo y estructura.
- Mantenedores de Usuarios, Actividades, Periodos y Dependencias con Agregar / Modificar / Eliminar.
- Asignaciones con selector múltiple de actividades por líder.
- Administrador puede modificar fechas de cualquier actividad sin estar asignado.
- Visualizador ve únicamente la opción Versiones.

## Archivos que deben actualizarse en GitHub

Reemplazar:
- app.py
- export_utils.py
- requirements.txt

Agregar o reemplazar:
- packages.txt
- templates/Calendario_Academico_Base_2027.xlsx

`db.py` y `seed_data.json` no requieren cambios para esta versión.

## Importante sobre Streamlit Cloud

`packages.txt` instala LibreOffice Calc en el servidor. Se utiliza para convertir la plantilla Excel al PDF de tres páginas manteniendo el diseño del archivo original. Después de subir estos archivos, Streamlit realizará un redeploy y puede tardar algunos minutos adicionales la primera vez.

## Prueba recomendada

1. Ingresar como Administrador.
2. Ir a Versiones y descargar PDF de V1.
3. Verificar que el PDF tenga 3 páginas y que la tercera (CA Gráfico 2027) esté horizontal.
4. Probar Usuarios, Actividades, Periodos y Dependencias en sus pestañas Agregar / Modificar / Eliminar.
5. Ir a Asignaciones, seleccionar un líder y asignar varias actividades desde el selector múltiple.
6. Ir a Calendario como Administrador y modificar cualquier actividad, aun cuando no esté asignada al usuario administrador.
7. Ingresar como Visualizador y comprobar que en el menú solo aparezca Versiones.
