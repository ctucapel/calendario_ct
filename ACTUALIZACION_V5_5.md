# Actualización V5.5

Cambios incorporados:

1. Vista Calendario incorpora **Líder responsable** y muestra todos los líderes asignados.
2. Se elimina **Día no hábil** y **Autor comentario**; se mantiene **Comentario**.
3. Domingos y feriados se destacan directamente en las celdas de fecha, con texto rojo y fondo suave.
4. Administrador edita cualquier actividad; Líder edita solo con **Actividades asignadas** activo.
5. Inicio usa cuatro tarjetas con estructura y tamaño homogéneos.
6. Mantenedor de Actividades mantiene búsqueda textual e incorpora filtro por **Grupo de actividad**.
7. Los ID visibles ahora son por periodo: `ACT-001-1`, `ACT-001-2`, `ACT-001-T`.
8. La misma actividad base puede visualizar varios ID, uno por cada periodo configurado.
9. Dependencias y asignaciones muestran los nuevos ID por periodo.
10. Comentarios: se muestran en una única columna. Cada usuario autorizado administra solo su propio comentario; otros comentarios son de solo lectura. Se permite más de un comentario por actividad/periodo cuando hay varios líderes.

## Archivos a reemplazar
- `app.py`
- `db.py`

No eliminar la base de datos. `db.py` migra automáticamente la tabla de comentarios existente.
