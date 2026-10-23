# Especificación de implementación — Correcciones de la capa experimental

**Estado:** propuesta para implementar; no es una declaración de fixes ya aplicados.
**Repositorio:** `Montinou/harness-engineering-patterns`
**ÚNICA rama de escritura:** `feat/experimental-evaluation-layer`
**Commit auditado y punto de partida confirmado:** `73bcb1306509dde7fff2f15bc43504b100f83984`
**Pull request existente:** draft #1, con base `main`.
**Fecha de preparación:** 22 de octubre de 2026.

## 0. Encargo y procedencia

Implementar las correcciones de la auditoría A1–A12 suministrada por el usuario para **esta rama**, mediante refactorización selectiva. Conservar las interfaces útiles, agregar pruebas adversariales y documentar las garantías reales. No fusionar con `main` al terminar.

La auditoría aportada reporta, entre otros resultados, 34 recomendaciones falsas en 400 simulaciones con cinco variantes sin efecto real y tres tareas; y una potencia de 15,7 % en otro escenario sintético. Esos números pertenecen a los escenarios del auditor: **no fueron reproducidos al redactar esta especificación y no son tasas universales para agentes reales**. Obtener los scripts originales si están disponibles; en su ausencia, recrear escenarios explícitos y llamarlos recreaciones, no réplicas exactas.

El encabezado de A1 llegó recortado, pero su reproducción está descrita: un `check.py` inicialmente fallido pasa después de que el solver lo reescribe. Esa reproducción es la base del requisito A1.

Se verificaron directamente la referencia de rama, el PR y las implementaciones actuales de `experiments/checks.py`, la ejecución/comparación de `experiments/core.py` y la persistencia de `core/store.py`. Los demás A1–A12 se toman como hallazgos del informe del usuario, con sus niveles de certeza originales. Las decisiones nuevas —contratos, valores por defecto, arquitectura de almacenamiento y criterios de aceptación— son **propuestas de esta especificación**, no afirmaciones del auditor.

### 0.1 Protección frente a otra confusión de ramas

Antes de cualquier escritura, registrar repositorio, directorio/worktree, rama y HEAD. Confirmar que el PR #1 tiene por head `feat/experimental-evaluation-layer`.

```bash
git remote -v
git status --short
git branch --show-current
git rev-parse HEAD
git worktree list --porcelain
```

La rama inicial debe contener el commit auditado. Si avanzó, inspeccionar y conservar sus cambios; no resetear ni sobrescribir trabajo ajeno. Si hay modificaciones locales no relacionadas, no descartarlas ni ocultarlas con un stash automático. Antes de publicar, comprobar otra vez el remoto y usar un push normal, nunca force-push.

**Prohibido:** escribir en `main`; crear otra implementación paralela; cambiar la rama base del PR; fusionar, cherry-pickear o reemplazar árboles completos desde `feat/harness-experimentation-layer`; borrar worktrees del usuario; cambiar permisos, secretos o infraestructura; ejecutar modelos/APIs de pago. La otra rama puede consultarse como referencia de ideas, pero también fue auditada con defectos. No es una fuente de código validado.

## 1. Objetivo, alcance y política de compatibilidad

El objetivo no es obtener más tests verdes, sino impedir decisiones de promoción basadas en un evaluador alterable, datos contaminados, métricas desconocidas o evidencia estadística insuficiente.

### Incluido

1. Integridad de los evaluadores y transporte seguro de artefactos.
2. Protocolo de desarrollo, validación y regresión, con consumo controlado del holdout.
3. Motor estadístico calibrado y estados de decisión explícitos.
4. Validación estricta de entradas, presupuestos y métricas confiables.
5. Persistencia transaccional, linaje consistente y evidencia incremental.
6. Entornos restringidos, límites de recursos, portabilidad y trazabilidad.
7. Migración de configuraciones/documentación y evidencia de regresión A1–A12.

### Fuera de alcance

No agregar Durable Harness, un dashboard web con servidor, orquestación distribuida, entrenamiento de modelos, despliegue automático ni integraciones comerciales completas con Claude Code/Codex/OpenCode. No se exige un experimento pagado para cerrar este trabajo. La ausencia de validación con modelos reales debe seguir apareciendo como limitación.

Conservar `harness-workbench`, `experiment`, `improve` y las extensiones existentes cuando sea razonable. El nuevo esquema de experimentos será versionado. Un manifiesto antiguo podrá ejecutarse como **exploratorio**, pero no deberá convertirse silenciosamente en evidencia confirmatoria. Agregar un comando o función de migración explícita; los splits y tests ocultos que no puedan inferirse deben solicitarse como configuración, no inventarse.

La versión de paquete en el punto de partida es 0.2.0. No declarar 0.3.0 por similitud con otra rama. Si se decide un cambio de versión por incompatibilidades, justificarlo en un ADR y sincronizar metadatos, CLI, README y changelog.

## 2. Contratos e invariantes obligatorios

### 2.1 Separar datos públicos de datos del controlador

Definir modelos tipados y serializables equivalentes a los siguientes; los nombres pueden adaptarse con un mapa de equivalencias:

| Contrato | Responsable y contenido |
|---|---|
| `PublicTrialRequest` | Tarea pública, configuración ejecutable del harness, fixture pública inmutable, presupuesto, identificador opaco y seed. |
| `PrivateExperimentPlan` | Splits/familias, candidatos congelados, rúbricas, resultados esperados, política estadística, presupuestos y hashes. Solo controlador. |
| `ExecutionResult` | Salida completa, referencia/hash de artefactos, resultado de ejecución y observaciones de consumo/tiempo con procedencia. |
| `EvaluationResult` | Hash de rúbrica y artefacto, resultados de checks, estado de evaluación y score solo cuando sea válido. |
| `ExperimentDecision` | Estado, razones, estimador, incertidumbre, plan/candidato/evidencia exactos y requisitos pendientes. |

No pasar al solver los tests ocultos, respuestas esperadas, etiquetas de split, resultados de otros candidatos, historial de validaciones ni métricas del linaje. El harness público contiene los componentes ejecutables necesarios, no todo su objeto administrativo. Usar identificadores opacos y conservar la correspondencia original únicamente en el controlador. Esto reduce canales explícitos; no promete que el contenido semántico de una tarea no revele su procedencia.

### 2.2 Estados, no booleanos ambiguos

Separar:

- `execution_status`: `completed`, `task_failure`, `execution_error`, `timeout`, `resource_limit`, `cancelled`.
- `evaluation_status`: `passed`, `failed`, `evaluation_error`, `not_evaluated`.
- `decision_status`: `eligible`, `rejected`, `insufficient_evidence`, `invalid_experiment`, `exploratory_only`.

Una solución equivocada es `failed`, no un fallo de infraestructura. Un ejecutable evaluador inexistente es `evaluation_error`; jamás `verified=true`. Un timeout de tarea sometida a un presupuesto explícito puede ser un fallo de la tarea si así se predefinió. No reclasificar después de ver qué candidato pierde. En la primera versión, cualquier error de infraestructura/evaluación sin resolver invalida la confirmación.

No omitir filas fallidas, no convertir errores del grader silenciosamente en ceros de calidad y no interpretar falta de evidencia como prueba de regresión. Guardar denominadores y motivos. Una promoción requiere, entre otras condiciones, evaluación válida y evidencia completa del plan predefinido.

## 3. FIX-01 — Integridad del evaluador y controles negativos

**Cubre:** A1, A5 y parte de A10/A12. **Bloqueante para aceptar resultados sobre código generado.**

### Diseño requerido

Congelar antes del experimento un `GraderBundle` con tests, rúbrica, dependencias, versión y hash. Mantenerlo fuera del workspace del solver y fuera de sus montajes accesibles. El solver nunca puede escribir ese bundle. El evaluador no usa como tests autoritativos archivos provistos o modificados por el solver.

Flujo:

```text
fixture pública -> solver -> finalización/cierre de sus procesos
                                 |
                           snapshot validado
                                 |
             evaluador nuevo + bundle confiable inmutable
                                 |
                  resultado emitido al controlador
```

Tras terminar el solver, crear una instantánea de artefactos verificando límites, rutas, tipos y hashes. Rechazar traversal, rutas absolutas, enlaces simbólicos/duros que escapen, archivos especiales y archivos/cantidades fuera del presupuesto. No evaluar un árbol que siga siendo modificado por procesos residuales.

El grader utiliza cwd, HOME, temporales y entorno propios; no carga módulos, plugins ni configuración de herramientas desde el artefacto por búsqueda implícita. Para Python, usar intérprete confiable y modo aislado donde corresponda. No resolver este requisito solo colocando `check.py` en otra carpeta: también hay que probar shadowing de `unittest.py`, `json.py`, `sitecustomize.py`, `conftest.py` y configuración/autoload de plugins.

**Límite importante:** `python -I` y tests read-only no convierten en confiable código del candidato importado en el mismo proceso que el juez. Si hay que ejecutar código generado, hacerlo en un worker restringido. Preferir que un supervisor confiable compare su resultado con el oracle fuera de ese worker. Cuando una suite de integración deba compartir proceso con el candidato, declarar esa limitación y probar manipulación del runner; no prometer resistencia general contra candidatos maliciosos.

Dos modos explícitos:

- `trusted_local`: depuración con código confiable y opt-in; no afirmar aislamiento de seguridad. Para ejecución de código no confiable, solo produce resultados exploratorios.
- `sandboxed`: backend con separación y límites comprobados. Necesario para promoción confirmatoria de tareas que ejecuten artefactos no confiables. Si el backend no existe o sus capacidades son insuficientes, rechazar antes de ejecutar; nunca degradar silenciosamente al host.

Implementar por lo menos la interfaz de capacidades y el bloqueo. Para cerrar A1 en el flujo de agentes de programación, entregar además una implementación offline verificable de evaluación restringida (por ejemplo, contenedor local). No requiere un agente/modelo real. Montar solo lo necesario, tests confiables read-only, usuario no privilegiado, red denegada por defecto, sin socket Docker ni acceso al repositorio/registro/credenciales del host. El supervisor y su registro no deben estar dentro del dominio escribible por el candidato.

### Checks y controles

`contains`/regex siguen disponibles, pero no son por sí solos prueba de corrección semántica. Cada rúbrica confirmatoria debe tener un control positivo y salidas/artefactos negativos conocidos. Validar ausencia de salida, salida incorrecta, enumeración de respuestas y artefactos defectuosos según el contrato.

No exigir que un check falle sobre el baseline: un baseline correcto debe poder pasar y los casos de regresión suelen existir por eso. Tampoco rechazar automáticamente salida vacía cuando el contrato legítimo permita vacío. Son controles semánticos por tarea, no reglas genéricas ciegas.

### Aceptación

- La solución equivocada que reescribe `check.py` continúa fallando; el hash de la rúbrica no cambia.
- Agregar `unittest.py` o plugins maliciosos no convierte fallo en éxito.
- Imprimir `PASS`, falsificar un JSON o salir con código cero sin ejecutar las comprobaciones exigidas no basta para fabricar un resultado del supervisor.
- Un test faltante, un grader que no arranca o un protocolo inválido produce `evaluation_error` y bloquea confirmación.
- Una solución correcta pasa por el mismo circuito.
- Los controles se ejecutan sobre la implementación real del evaluador; no únicamente sobre mocks.

## 4. FIX-02 — Desarrollo, holdout, regresión y reutilización

**Cubre:** A2, A3, A9.

Agregar `mode: exploratory|confirmatory`, `dataset_id`, `dataset_version`, `split`, `family_id` y fingerprints de contenido. Un benchmark confirmatorio necesita desarrollo, validación y regresión; un manifiesto antiguo sin splits queda en modo exploratorio y no habilita promoción validada.

Separar por contenido y familia, no solamente por IDs. Rechazar duplicados exactos entre splits y exigir revisión de familias/casos casi duplicados. Hashes no prueban independencia semántica.

Protocolo:

1. Proponer/modificar/rankear solo con desarrollo.
2. Congelar una única finalista, el baseline, los parámetros, la métrica y el presupuesto antes de validar.
3. Persistir `selection_frozen` con hashes completos.
4. Ejecutar únicamente esa finalista y el baseline sobre el holdout y regresión.
5. Emitir una decisión sin probar la segunda candidata después de ver un resultado desfavorable.

Una composición es otra hipótesis de desarrollo. Debe congelarse y evaluarse como la configuración exacta que se pretende promover. Las mejoras secuenciales pueden usar desarrollo en cada paso, pero la confirmación ocurre una sola vez sobre la candidata final congelada; no reutilizar automáticamente el holdout dentro del ciclo `improve`.

### Registro de exposición

Registrar uso de holdouts por fingerprint de contenido, familia experimental, candidato y política. Reservar el holdout transaccionalmente antes de su primera ejecución. Si sus resultados ya se usaron para modificar o seleccionar candidatos, nuevas variantes no podrán tratarlo como confirmatorio: requerir otro holdout o marcar la ejecución como exploratoria. Renombrar IDs no evita la detección.

Una relectura/regeneración de reportes no consume nuevos datos. Un retry de infraestructura no crea muestras adicionales ni autoriza repetir hasta obtener éxito. Cualquier política de retries se fija antes de ejecutar, conserva todos los intentos y no permite cambiar código, rúbrica o candidato a mitad de la confirmación.

Este registro tiene alcance local/compartido del proyecto: no detecta universalmente reutilización en otro equipo ni exposición al preentrenamiento del modelo. Documentar esa limitación.

### Aceptación

- Cinco variantes sin efecto compiten solo en desarrollo; exactamente una pasa a confirmación.
- Cambiar cualquier parámetro del candidato después de congelarlo invalida la confirmación.
- Una tarea repetida 5 o 1.000 veces no cumple el mínimo de unidades independientes.
- El proposer no recibe tareas ni resultados del holdout; verificar con un spy del contrato real.
- La segunda confirmación adaptativa sobre un holdout ya expuesto se bloquea, incluso renombrando tareas.

## 5. FIX-03 — Estadística explícita y calibración de potencia

**Cubre:** A2, A3, A8 y parte de A9.

No portar sin análisis el test de signos de la otra rama. No agregar múltiples condiciones estadísticas redundantes sin medir su efecto sobre la potencia. Esta especificación propone un punto de partida, condicionado a calibración; no una garantía universal.

### 5.1 Estimando y unidad independiente

Predefinir una métrica primaria y su ponderación. Para cada tarea, calcular la media de sus diferencias candidato-baseline sobre las repeticiones pareadas. Para familias correlacionadas, agregar dentro de familia y usar esa familia como unidad independiente. La política inicial propuesta da igual peso a cada unidad independiente; cualquier ponderación alternativa debe congelarse antes de obtener resultados y aparecer en el informe.

```text
d_task = mean_repeticiones(score_candidato - score_baseline)
d_family = mean_tareas_de_familia(d_task)
effect = mean_familias(d_family)
```

Si cada tarea constituye una familia independiente, ambas fórmulas se reducen al promedio pareado por tarea. Ni checks, ni pasos, ni retries, ni repeticiones aumentan `n_independent`. Registrar las hipótesis de independencia y la población a la que se intenta generalizar.

### 5.2 Política inicial propuesta

- Hipótesis primaria: `H0: effect <= practical_margin`; `H1: effect > practical_margin`.
- Valores iniciales propuestos: `alpha=0.05`, `practical_margin=0.05` para score normalizado.
- Intervalo unilateral inferior mediante bootstrap de **unidades independientes completas**, conservando los pares. No aplanar ejecuciones como observaciones independientes ni duplicar capas de variabilidad sin justificación.
- Inferencia inicial `cluster_percentile_ci_v1`, con al menos 10.000 remuestreos finales; contrastarla con una implementación de referencia. Si no calibra, cambiar mediante ADR a una alternativa justificada, no ajustar thresholds después de ver el resultado real.
- Propuesta de suelo operativo: 30 unidades independientes en validación confirmatoria. Es una política conservadora de ingeniería, **no un tamaño estadísticamente suficiente por definición**. El tamaño definitivo proviene del análisis de potencia del perfil.
- Una comparación confirmatoria, métrica primaria predefinida. Las demás métricas son diagnósticas o condiciones predefinidas, no oportunidades alternativas de declarar éxito.
- En esta iteración, no soportar múltiples finalistas confirmatorios. Rechazar esa configuración. Una extensión futura necesita corrección de multiplicidad y nueva calibración.

Promoción estadística solo si el límite inferior supera el margen práctico, el tamaño/plan completo coincide con el perfil y no existe un bloqueo de integridad. Un intervalo indefinido, degenerado sin justificación o que no permite concluir produce `insufficient_evidence`; nunca se convierte en aprobación por conveniencia.

### 5.3 Regresiones sin veto arbitrario por ruido

Separar dos tipos:

- **Invariantes críticos:** deterministas y predefinidos; un incumplimiento real veta la candidata.
- **Calidad estocástica:** no inferioridad con tolerancia `regression_margin` fijada por dominio antes de ejecutar. Para cada grupo de regresión predefinido, exigir evidencia suficiente de `effect_regression > -regression_margin`. No confundir una pequeña caída puntual con una regresión demostrada; incertidumbre amplia significa evidencia insuficiente.

Cuando se reporten límites simultáneos para varios grupos protegidos, ajustar sus niveles (propuesta inicial: Bonferroni entre esos grupos) y recalibrar potencia **del protocolo completo**. No crear un grupo estadístico por cada uno de cientos de tests salvo justificación de tamaño muestral. Tolerancia cero no será un default oculto para resultados ruidosos. Debe elegirse explícitamente o quedar sin confirmación.

### 5.4 Ensayo Monte Carlo obligatorio

Implementar un simulador rápido que ejecute las mismas funciones reales de selección y decisión, sin modelos ni subprocess por observación. Publicar supuestos, seeds, parámetros, código y resultados, separando calibración de evaluación final del simulador.

Matriz mínima: 1/5/20 candidatos de desarrollo; pocas/muchas unidades; 1/3/5/10 repeticiones; resultados binarios y scores continuos acotados; dificultades heterogéneas; correlación por familia; mejora concentrada en pocas tareas; ausencia de mejora; efecto exactamente en el margen; regresión y missingness/errores de infraestructura.

Recrear como diagnósticos los escenarios descritos en A2/A3/A8. No exigir igualdad de porcentajes sin los scripts originales ni declarar que un resultado sintético representa la distribución de LLMs.

**Criterios de aceptación propuestos para el perfil publicado:**

- Al menos 5.000 simulaciones por escenario nulo clave y 2.000 por escenario de potencia clave en la validación final offline.
- Con `alpha=0.05`, límite superior unilateral del 95 % de la frecuencia de falsa promoción no mayor a 0.06 en cada escenario nulo soportado, incluida la frontera `effect=practical_margin`. El 0.01 adicional es tolerancia Monte Carlo/de ingeniería documentada; no un aumento encubierto del alpha.
- Para un escenario objetivo predefinido de éxito 0.50 → 0.70, margen práctico 0.05 y regresiones dentro de tolerancia, límite inferior del 95 % de la potencia **del circuito completo** al menos 0.80 en un plan de tamaño finito documentado. Publicar N/R/costo requerido; no prometer que 5 tareas alcanzan.
- Con candidatos de desarrollo adicionales, el error del protocolo completo no debe exceder el límite anterior.
- Las configuraciones pequeñas no soportadas devuelven `insufficient_evidence` en confirmación, aunque sigan siendo útiles en exploración.

Si el método inicial no pasa, aumentar el plan de muestreo o justificar otro método. No debilitar los límites ni elegir seeds favorables para que pase. Un informe que rechaza todos los casos **no** satisface la especificación: también debe superar la condición de potencia.

## 6. FIX-04 — Valores válidos, consumo y tiempo confiables

**Cubre:** A4, A5, A6 y parte de A12.

### Validación

Rechazar `NaN`, `Infinity`, `-Infinity` al cargar y guardar JSON, incluidos valores anidados. Validar además objetos creados directamente mediante API Python, no solo el parser JSON. Los campos numéricos requieren tipo, finitud y rango correctos; `bool` no cuenta como entero. No corregir silenciosamente entradas inválidas con coerciones o clamps.

Aplicar a márgenes, pesos, confianza/alpha, proporciones, límites de costos, latencias, repeticiones, workers, timeouts, seeds y scores. Errores de manifiesto se detectan antes de llamadas/subprocess. Conservar los nombres antiguos como aliases de migración solo si no son contradictorios; por ejemplo, distinguir explícitamente presupuesto de tokens de costo monetario.

### Métricas

Representar consumo como valor nullable más estado/procedencia: `measured`, `provider_reported`, `adapter_reported`, `estimated`, `unknown`, `not_applicable`. `0` es cero conocido; ausencia no es cero. No permitir que el proceso evaluado autodeclare un nivel de confianza mayor para su consumo.

Medir latencia con reloj monotónico en el supervisor; la latencia reportada por el agente queda como diagnóstico. Preservar todos los consumos observados, incluidos fallos, jueces, verificadores y retries; indicar costos excluidos, como propuestas externas no instrumentadas.

Un gate habilitado de costo/tiempo con datos desconocidos, no finitos o sin procedencia aceptable bloquea la confirmación. Si el baseline conocido es cero, el cociente es indefinido: bloquear salvo que exista una política absoluta alternativa declarada antes de ejecutar. No omitir el gate porque el cociente sea `None`.

Cuando se comparen modelos con precios distintos, tokens no equivalen a dinero. Puede mantenerse un límite de tokens, etiquetado como tal; un límite monetario necesita modelo de precios versionado. Sin observación confiable, no declarar costo verificado.

### Aceptación

- Baseline 0/desconocido frente a candidato de 10 millones de tokens no aprueba silenciosamente.
- NaN en cada parámetro crítico es rechazado antes de ejecutar; NaN en un reporte invalida ese resultado.
- Un agente que reporta latencia 0 no reduce la medida del supervisor.
- Un verificador inexistente no aumenta `verified_runs` ni se presenta como fallo ordinario de la solución.
- Missingness, métricas negativas, strings inesperados, booleanos y out-of-range tienen tests separados.

## 7. FIX-05 — Persistencia, concurrencia y linaje

**Cubre:** A7 y la persistencia parcial de A12.

Propuesta: usar SQLite de la biblioteca estándar como fuente de verdad local para objetos, identidad de versiones, experimentos, intentos, evaluaciones, decisiones, exposición de holdouts y promociones. JSON/Markdown/HTML quedan como vistas exportables. No importar la implementación alternativa ni asumir que sus métodos de promoción son correctos.

Separar `execution_config_hash` —componentes que cambian la ejecución— del descriptor administrativo/linaje. Registrar ambos. La identidad pública `(harness_id, version)` del baseline se registra una vez: si existe con configuración compatible, se reutiliza; si la configuración difiere, error de conflicto. **Nunca renumerar el baseline para resolver una carrera.**

Asignar identidad al candidato, parent y evidencia dentro de transacciones cortas con restricciones únicas/foráneas. Los subprocess y llamadas de red no mantienen abierto un lock de escritura. Tratar `SQLITE_BUSY` con límites y error explícito, no con bucles infinitos.

Promoción ligada al hash del candidato que efectivamente se evaluó, al plan congelado y a su decisión final. Evitar API `promote(harness_arbitrario)` que permita sustituir el candidato. Una promoción por `decision_id` debe ser idempotente: devuelve la existente o falla por conflicto semántico, nunca genera una versión nueva por retry.

Guardar cada ejecución y evaluación al completarse. Un proceso interrumpido deja evidencia inspeccionable y experimento incompleto/no elegible. La reanudación automática no es requisito; no simularla con nuevas muestras seleccionadas hasta pasar.

Las exportaciones legacy se generan después de commit y se publican mediante reemplazo atómico. Si falla la exportación, la DB conserva el estado confirmado y permite regenerar la vista. No describir un lock SQLite alrededor de archivos sueltos como una transacción atómica de todos esos archivos.

Migración explícita, idempotente, con validación/dry-run y copia de seguridad; no eliminar ni reescribir evidencia vieja. Hashes detectan inconsistencia accidental cuando se comprueban, no garantizan inviolabilidad ante un administrador que puede alterar DB y hashes.

### Aceptación

- Seis y doce **procesos** simultáneos parten de la misma v1: existe una sola v1 canónica, sin bases fantasma renumeradas.
- Todos los hijos legítimos referencian esa base, nunca una copia creada por una carrera.
- Reintentar una misma promoción no duplica hijo ni evento de aprobación.
- Identidad de base igual con contenido distinto provoca conflicto visible.
- Kill antes/después de puntos transaccionales no deja decisión aprobada sin evidencia/candidato correspondiente.
- Una exportación fallida se regenera sin crear versiones nuevas.
- Tests usan sincronización/barreras para provocar la carrera, no solo sleeps y threads.

## 8. FIX-06 — Entorno, procesos y secretos

**Cubre:** A10 y riesgos de A12. El timeout del proceso nieto no fue reproducido por el auditor; probarlo antes de afirmarlo como defecto demostrado.

No heredar `os.environ` completo. El solver recibe una allowlist mínima y secretos explícitamente autorizados para su tarea; el grader no recibe credenciales del proveedor. HOME, cachés, temporales y estado de sesión son nuevos por trial. Eliminar herencia implícita de PYTHONPATH/plugin paths. No guardar valores de secretos en manifiestos ni logs.

Usar argv, no shell implícito. Establecer límites de tiempo, stdout/stderr, tamaño/cantidad de archivos y recursos. Capturar streams de forma acotada, no esperar al final para descubrir que se agotó memoria. Probar límites en ejecución real.

En backend restringido, terminar todos los procesos del contenedor/job antes de capturar artefactos. En local, documentar que process groups no contienen necesariamente descendientes que crean nuevas sesiones; no anunciar un sandbox por ejecutar killpg. Toda plataforma sin las capacidades exigidas falla explícitamente en modo estricto.

**Aceptación:** un secreto canario del entorno padre no aparece en la salida del solver ni del grader sin autorización; output ilimitado termina en `resource_limit`; procesos residuales no modifican el snapshot después del cierre en backend restringido; no se monta el Docker socket en el worker. Estos tests no deben usar secretos reales.

## 9. FIX-07 — Portabilidad y trazabilidad

**Cubre:** A11, A12.

### Portabilidad

El código interno usa `sys.executable`, y los manifiestos pueden usar `{python}` resuelto al intérprete configurado por el controlador. No depender del comando global `python`, del cwd del usuario ni de un paquete instalado casualmente. Resolver recursos relativos al manifiesto y scripts de demo mediante ubicaciones confiables explícitas.

La suite debe pasar desde un checkout limpio sin instalación editable global. La demo puede usar un script confiable de biblioteca estándar; no corregir imports agregando el workspace del candidato a PYTHONPATH. Agregar tests de instalación en venv con wheel y con editable, más ejecución desde un directorio ajeno al checkout y con PATH sin alias `python`.

CI propuesto: Python 3.10, 3.12 y 3.13 para el núcleo; pruebas de aislamiento real en Linux con capacidades declaradas. Plataformas no probadas se declaran no verificadas, no compatibles por inferencia. No marcar A11 resuelto solo porque CI instala el paquete antes.

### Trazabilidad

Guardar: commit y árbol/dirty diff de implementación; schema version; plan/harness/fixtures/rúbricas congelados; ejecutable y adaptador; lockfile/dependencias o digest de imagen; requested/returned model ID cuando se disponga; seeds y su alcance; procesos/entorno autorizado; límites; outcomes; costos/tiempo y procedencia.

Un cambio de código declarado, modelo retornado o rúbrica entre trials invalida la confirmación. Si un proveedor no permite identificar una versión estable, marcarla desconocida y limitar la afirmación de reproducibilidad. Un seed común no hace determinista ni realmente pareado por aleatoriedad a un proveedor que no lo respeta.

No afirmar que un hash del script cubre todas sus importaciones. Registrar un paquete/lock/image que cubra dependencias o declarar el alcance parcial. Las capacidades soportadas del adaptador deben ser explícitas: prompts, herramientas, memoria, subagentes, scripts, plugins. En modo confirmatorio, una variante que cambia un componente no soportado se rechaza antes de ejecutar. El backend de texto puede estudiar prompts, no demostrar mejora de herramientas que nunca invoca.

## 10. Pruebas de regresión y orden de implementación

Conservar las 36 pruebas existentes, salvo cambios de expectativas documentados por nueva semántica. No eliminar tests solo para obtener verde. Primero escribir reproducciones del defecto actual y registrar rojo; después corregir y registrar verde. Un hallazgo que no se reproduzca se informa, sin fabricar evidencia.

| Hallazgo | Prueba mínima nueva / resultado esperado |
|---|---|
| A1 | Reescritura de check.py y shadowing no transforman solución incorrecta en válida. |
| A2 | Selección solo en desarrollo; finalista congelada; simulación completa de falsos positivos. |
| A3 | Una tarea con numerosas repeticiones no es confirmatoria. |
| A4 | Costo desconocido/cero sin fallback predefinido bloquea; latencia externa no es autoridad. |
| A5 | Grader inexistente: evaluation_error y verified=false. |
| A6 | NaN/Inf/booleanos/valores fuera de rango rechazados en JSON y API. |
| A7 | Multiproceso sincronizado deja un único baseline y linaje correcto. |
| A8 | Perfil estadístico supera umbrales de error y potencia con intervalos Monte Carlo. |
| A9 | Reutilización adaptativa del holdout, incluso con IDs nuevos, pierde confirmación. |
| A10 | Secreto canario no se hereda; credenciales solver/grader separadas. |
| A11 | Checkout y venv limpios, cwd distinto, PATH sin python: tests y demo funcionan. |
| A12 | Drift detectado, evidencia incremental conservada y streams limitados. |

Orden propuesto de commits lógicos:

1. Reproducciones, estados y validación de esquema.
2. Evaluador protegido, supervisor/artefactos y entornos.
3. Registro transaccional, linaje y exposición de holdouts.
4. Protocolo de splits/finalista e integración no contaminante con Improve.
5. Inferencia, no inferioridad y calibración Monte Carlo.
6. Presupuestos, trazabilidad, portabilidad y compatibilidad.
7. Documentación, ejemplos y pruebas de integración completas.

La implementación puede reordenar dependencias, pero debe registrar el motivo. Mantener los cambios exclusivamente en la rama autorizada. No reemplazar el PR por otro ni fusionarlo.

## 11. Evidencia, documentación y Definition of Done

Entregar en la rama:

- Código y regresiones A1–A12 con separación unit/integration/adversarial/statistical.
- `docs/audit-remediation.md`: mapa A1–A12 → cambio → test → resultado → limitaciones.
- ADR del límite de confianza del evaluador; ADR del protocolo estadístico; ADR de persistencia/migración.
- `docs/statistical-validation.md`: estimando, hipótesis, muestreo, tamaños, escenarios, FPR/potencia con intervalos, seeds y límites de extrapolación.
- README, `docs/workbench.md`, `docs/experiments.md`, `docs/verification.md`, changelog y ejemplos coherentes con el HEAD final de **esta rama**.
- Reportes JSON/HTML que distingan exploración, evidencia insuficiente, error del verificador, rechazo y elegibilidad. Los datos sintéticos se etiquetan y nunca prueban mejora de modelos reales.
- Registro de comandos ejecutados, versiones, salidas verificables, tests omitidos y motivo. No atribuir resultados locales a CI ni fabricar IDs de ejecución.

Criterio de cierre:

1. Los casos adversariales A1 y las rutas de NaN/errores/consumo no pueden aprobar silenciosamente.
2. Selección y confirmación están separadas y los holdouts usados no se reutilizan adaptativamente como nuevos.
3. Existe al menos un perfil estadístico publicado que cumple ambos objetivos: control de falsas recomendaciones y potencia suficiente para su efecto objetivo, con N/R explícitos.
4. El linaje multiproceso y la asociación decisión-candidato son consistentes e idempotentes.
5. Los procesos no reciben secretos implícitos; límites y capacidades se verifican o bloquean.
6. Los 36 tests previos y los nuevos pasan en los entornos declarados; los tests de aislamiento requeridos no se cuentan como pasados si fueron omitidos.
7. El PR #1 se mantiene draft y `main` sin cambios hasta una revisión independiente del **nuevo SHA**.

No declarar todo cerrado si el resultado es únicamente documentación honesta de una vulnerabilidad que sigue habilitando promoción. Para capacidades no implementadas, cerrar la ruta mediante un rechazo explícito y documentar el alcance restante. Un bloqueo seguro puede habilitar la integración experimental del núcleo, pero no autoriza afirmar soporte para agentes de código no confiables hasta superar sus tests de aislamiento.

## 12. Referencias y cómo utilizarlas

**Evidencia del proyecto:** auditoría A1–A12 pegada por el usuario; rama y PR leídos al preparar este documento; código fuente fijado en 73bcb1306509dde7fff2f15bc43504b100f83984. No usar los hallazgos F1–F11 de la auditoría de otra rama como reproducciones de ésta.

**Referencias externas consultadas para verificar decisiones de diseño, no como sustituto de la auditoría:**

- Python, opciones de línea de comandos y modo aislado: https://docs.python.org/3/using/cmdline.html
- SciPy, bootstrap y preservación de pares: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html
- NIST, hipótesis, potencia y significancia práctica: https://www.itl.nist.gov/div898/handbook/eda/section3/eda35.htm
- NIST, planificación del tamaño muestral: https://www.itl.nist.gov/div898/handbook/prc/section2/prc242.htm
- SQLite, transacciones: https://www.sqlite.org/lang_transaction.html
- Docker, ejecución y controles de contenedores: https://docs.docker.com/engine/containers/run/

Estas referencias describen mecanismos; no prueban que el futuro código los use correctamente. La aceptación depende de las pruebas y la evidencia exigidas en esta especificación.
