"""Generador de rutinas de gimnasio para uso personal, pensado para Safari en iPhone."""

import hmac
import json
import random
import re
import urllib.request

import openai
import streamlit as st

# Dataset fijado a un commit para que un cambio en el repo original no rompa la app.
# Los datos son MIT, pero los GIFs son © Gym visual y su licencia no permite
# redistribuirlos: por eso se cargan desde el repo original en vez de copiarlos aquí.
DATASET = "https://raw.githubusercontent.com/hasaneyldrm/exercises-dataset/7455efae41b330c265e7cd4b78dfa848e7ce5ebd"
ATRIBUCION = "© Gym visual — https://gymvisual.com/"
MODELO = "deepseek-flash"

FULL_BODY = "Full body"
# Opción del formulario -> músculos objetivo ("target") del dataset, con su nombre en español.
GRUPOS = {
    "Pecho": {"pectorals": "Pectorales", "serratus anterior": "Serrato"},
    "Espalda": {"lats": "Dorsales", "upper back": "Espalda alta", "traps": "Trapecios", "spine": "Lumbares"},
    "Hombros": {"delts": "Deltoides"},
    "Bíceps": {"biceps": "Bíceps"},
    "Tríceps": {"triceps": "Tríceps"},
    "Antebrazos": {"forearms": "Antebrazos"},
    "Pierna": {
        "quads": "Cuádriceps", "hamstrings": "Isquiotibiales", "glutes": "Glúteos",
        "adductors": "Aductores", "abductors": "Abductores", "calves": "Pantorrillas",
    },
    "Abdomen": {"abs": "Abdomen"},
    "Cardio": {"cardiovascular system": "Cardio"},
}
MUSCULO_ES = {target: nombre for grupo in GRUPOS.values() for target, nombre in grupo.items()}

# Opción del formulario -> valores de "equipment" del dataset.
EQUIPOS = {
    "Peso corporal": {"body weight"},
    "Mancuernas": {"dumbbell"},
    "Barra": {"barbell", "ez barbell", "olympic barbell", "trap bar"},
    "Poleas": {"cable"},
    "Máquinas": {
        "leverage machine", "smith machine", "sled machine", "stationary bike",
        "elliptical machine", "stepmill machine", "skierg machine", "upper body ergometer",
    },
    "Kettlebell": {"kettlebell"},
    "Bandas": {"band", "resistance band"},
    "Otros": {
        "weighted", "assisted", "stability ball", "medicine ball", "bosu ball",
        "rope", "roller", "wheel roller", "hammer", "tire",
    },
}

# Minutos -> cuántos ejercicios pedir.
DURACIONES = {30: (4, 5), 45: (5, 6), 60: (6, 7), 90: (8, 10)}
NIVELES = ["Principiante", "Intermedio", "Avanzado"]
OPCIONES_INICIALES = {"grupos": [FULL_BODY], "equipo": list(EQUIPOS), "duracion": 60, "nivel": "Intermedio"}

SISTEMA = """Eres un entrenador personal. Armas rutinas de gimnasio usando solo ejercicios de la lista que te da el usuario.
Responde únicamente con un objeto JSON con esta forma:
{"ejercicios": [{"id": "0025", "series": 4, "repeticiones": "8-10", "descanso": 90}]}
- "id": el id de 4 dígitos tal como aparece en la lista. Nunca inventes ids ni uses ejercicios fuera de la lista.
- "series": número entero.
- "repeticiones": texto corto, como "8-10", "12" o "30 s" si el ejercicio es por tiempo.
- "descanso": segundos de descanso entre series, número entero.
Ordena los ejercicios como se harían en el gimnasio: primero los compuestos, después los de aislamiento y al final core o cardio.
Reparte el trabajo entre los grupos musculares pedidos y no repitas ejercicios ni variantes casi iguales.
Adapta al nivel la complejidad de los ejercicios (máquinas y movimientos simples para principiantes), el volumen y los descansos."""


def secreto(nombre):
    try:
        return st.secrets[nombre]
    except (KeyError, FileNotFoundError):
        st.error(f"Falta el secret `{nombre}`. Agrégalo en Streamlit Cloud, en Settings → Secrets.")
        st.stop()


def pedir_contrasena():
    """Corta la ejecución hasta que se ingrese la contraseña correcta."""
    if st.session_state.get("autenticado"):
        return
    clave = str(secreto("APP_PASSWORD"))
    st.title("🏋️ Rutinas")
    with st.form("login"):
        intento = st.text_input("Contraseña", type="password", autocomplete="current-password")
        if st.form_submit_button("Entrar", type="primary", width="stretch"):
            # Se comparan bytes porque compare_digest no acepta str con tildes o ñ.
            if hmac.compare_digest(intento.encode(), clave.encode()):
                st.session_state.autenticado = True
                st.rerun()
            st.error("Contraseña incorrecta.")
    st.stop()


@st.cache_data(show_spinner="Cargando ejercicios…")
def cargar_ejercicios():
    """Descarga el dataset y deja solo los campos que usa la app, indexado por id."""
    with urllib.request.urlopen(f"{DATASET}/data/exercises.json", timeout=60) as respuesta:
        datos = json.load(respuesta)
    return {
        e["id"]: {
            "id": e["id"],
            # Algunos nombres traen "45в°" en vez de "45°" (error de encoding del dataset).
            "nombre": e["name"].replace("в°", "°"),
            "musculo": e["target"],
            "equipo": e["equipment"],
            "gif": f"{DATASET}/{e['gif_url']}",
            "pasos": e["instruction_steps"].get("es") or e["instruction_steps"]["en"],
        }
        for e in datos
    }


def filtrar(ejercicios, opciones):
    """Ejercicios que encajan con los músculos y el equipo elegidos."""
    if FULL_BODY in opciones["grupos"]:
        musculos = set(MUSCULO_ES)
    else:
        musculos = {target for grupo in opciones["grupos"] for target in GRUPOS[grupo]}
    equipos = set().union(*(EQUIPOS[nombre] for nombre in opciones["equipo"]))
    return [
        e for e in ejercicios.values()
        if e["musculo"] in musculos and e["equipo"] in equipos
        # Los estiramientos no encajan en una rutina de series x repeticiones.
        and "stretch" not in e["nombre"].lower()
    ]


def numero(valor, defecto, minimo, maximo):
    """Entero dentro de [minimo, maximo]; acepta respuestas como 90, "90" o "90 s"."""
    encontrado = re.search(r"\d+", str(valor))
    return min(max(int(encontrado.group()), minimo), maximo) if encontrado else defecto


def validar(items, validos):
    """Normaliza la rutina del modelo. Devuelve (rutina, cantidad de ids descartados)."""
    rutina, vistos, descartados = [], set(), 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        id_ = str(item.get("id", "")).strip().zfill(4)
        if id_ not in validos:
            descartados += 1
        elif id_ not in vistos:
            vistos.add(id_)
            rutina.append({
                "id": id_,
                "series": numero(item.get("series"), 3, 1, 10),
                # Sin caracteres que rompan el markdown de la tarjeta.
                "repeticiones": re.sub(r"[\[\]$*_`\\]", "", str(item.get("repeticiones") or "10"))[:20],
                "descanso": numero(item.get("descanso"), 60, 15, 600),
            })
    return rutina, descartados


def pedir_rutina(opciones, candidatos, evitar):
    """Le pide la rutina a DeepSeek y la devuelve validada contra los candidatos."""
    minimo, maximo = DURACIONES[opciones["duracion"]]
    pedido = [
        f"Qué entrenar: {', '.join(opciones['grupos'])}",
        f"Equipo disponible: {', '.join(opciones['equipo'])}",
        f"Duración aproximada: {opciones['duracion']} minutos (entre {minimo} y {maximo} ejercicios)",
        f"Nivel: {opciones['nivel']}",
    ]
    if evitar:
        pedido.append(f"Para variar, evita en lo posible estos ids de la rutina anterior: {', '.join(evitar)}")
    # Mezclar y después agrupar por músculo: la lista queda ordenada, pero cambia en cada pedido.
    lista = random.sample(candidatos, len(candidatos))
    lista.sort(key=lambda e: e["musculo"])
    pedido.append("\nEjercicios disponibles (id | nombre | músculo | equipo):")
    pedido += [f"{e['id']} | {e['nombre']} | {e['musculo']} | {e['equipo']}" for e in lista]

    cliente = openai.OpenAI(api_key=secreto("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com", timeout=60)
    mensajes = [{"role": "system", "content": SISTEMA}, {"role": "user", "content": "\n".join(pedido)}]
    validos = {e["id"] for e in candidatos}
    for _ in range(2):  # DeepSeek a veces devuelve una respuesta vacía: se reintenta una vez.
        respuesta = cliente.chat.completions.create(
            model=MODELO,
            messages=mensajes,
            response_format={"type": "json_object"},
            max_tokens=2000,
            # El modo "thinking" viene activado por defecto; sin él responde mucho más rápido.
            extra_body={"thinking": {"type": "disabled"}},
        )
        try:
            items = json.loads(respuesta.choices[0].message.content or "")["ejercicios"]
        except (ValueError, KeyError, TypeError):
            continue
        rutina, descartados = validar(items, validos)
        if rutina:
            return rutina, descartados
    raise ValueError("DeepSeek no devolvió una rutina válida. Prueba de nuevo.")


def guardar(opciones, rutina, descartados=0):
    st.session_state.update(opciones=opciones, rutina=rutina, descartados=descartados)
    # También en la URL: si Safari recarga la página (por ejemplo, tras un rato con el
    # iPhone bloqueado), la rutina se recupera sin volver a llamar a DeepSeek.
    st.query_params["r"] = json.dumps({"o": opciones, "r": rutina}, ensure_ascii=False, separators=(",", ":"))


def restaurar(ejercicios):
    """Recupera la rutina guardada en la URL al empezar una sesión nueva."""
    try:
        guardado = json.loads(st.query_params["r"])
        opciones = guardado["o"]
        rutina, _ = validar(guardado["r"], ejercicios)
        if (
            rutina and opciones["grupos"] and opciones["equipo"]
            and set(opciones["grupos"]) <= {FULL_BODY, *GRUPOS}
            and set(opciones["equipo"]) <= set(EQUIPOS)
            and opciones["duracion"] in DURACIONES
            and opciones["nivel"] in NIVELES
        ):
            st.session_state.update(opciones=opciones, rutina=rutina)
    except (KeyError, ValueError, TypeError):
        pass  # Sin rutina en la URL, o de una versión anterior de la app: se empieza de cero.


def generar(ejercicios, opciones, evitar=()):
    """Arma y guarda una rutina nueva. Devuelve False si falló (el error ya quedó en pantalla)."""
    candidatos = filtrar(ejercicios, opciones)
    if not candidatos:
        st.warning("No hay ejercicios para esa combinación de músculos y equipo.")
        return False
    try:
        with st.spinner("Armando tu rutina…"):
            rutina, descartados = pedir_rutina(opciones, candidatos, evitar)
    except openai.AuthenticationError:
        st.error("DeepSeek rechazó la API key. Revisa `DEEPSEEK_API_KEY` en los secrets.")
        return False
    except (openai.APIError, ValueError) as error:
        st.error(f"No se pudo generar la rutina: {error}")
        return False
    guardar(opciones, rutina, descartados)
    return True


def formato_descanso(segundos):
    minutos, resto = divmod(segundos, 60)
    if not minutos:
        return f"{resto} s"
    return f"{minutos} min {resto} s" if resto else f"{minutos} min"


def pantalla_formulario(ejercicios):
    st.title("🏋️ Rutinas")
    o = st.session_state.opciones or OPCIONES_INICIALES
    with st.form("formulario"):
        grupos = st.pills("¿Qué quieres entrenar?", [FULL_BODY, *GRUPOS], selection_mode="multi", default=o["grupos"])
        equipo = st.pills(
            "Equipo disponible", list(EQUIPOS), selection_mode="multi", default=o["equipo"],
            help="Otros: discos o lastre, fitball, balón medicinal, bosu, cuerda, rodillo…",
        )
        duracion = st.segmented_control(
            "Duración", list(DURACIONES), default=o["duracion"], required=True,
            format_func=lambda minutos: f"{minutos} min", width="stretch",
        )
        nivel = st.segmented_control("Nivel", NIVELES, default=o["nivel"], required=True, width="stretch")
        enviado = st.form_submit_button("Generar rutina", type="primary", icon=":material/bolt:", width="stretch")
    if enviado:
        if not grupos or not equipo:
            st.warning("Elige al menos un grupo muscular y un tipo de equipo.")
        elif generar(ejercicios, {"grupos": grupos, "equipo": equipo, "duracion": duracion, "nivel": nivel}):
            st.rerun()


def tarjeta(posicion, item, ejercicio):
    with st.container(border=True):
        nombre = ejercicio["nombre"]
        st.markdown(f"#### {posicion}. {nombre[:1].upper()}{nombre[1:]}", anchors=False)
        st.markdown(
            f":blue-badge[{item['series']} × {item['repeticiones']}] "
            f":gray-badge[:material/timer: Descanso {formato_descanso(item['descanso'])}] "
            f":violet-badge[{MUSCULO_ES.get(ejercicio['musculo'], ejercicio['musculo'])}]"
        )
        # Los "videos" del dataset son GIFs: se reproducen solos, en loop, sin sonido
        # y dentro de la página en Safari, sin pasar a pantalla completa.
        with st.container(horizontal_alignment="center"):
            st.image(ejercicio["gif"], caption=ATRIBUCION, width=260)
        with st.expander("Instrucciones"):
            st.markdown("\n".join(f"{n}. {paso}" for n, paso in enumerate(ejercicio["pasos"], 1)))


def pantalla_rutina(ejercicios):
    o = st.session_state.opciones
    st.title("🏋️ Tu rutina")
    st.caption(f"{', '.join(o['grupos'])} · {o['duracion']} min · {o['nivel']}")
    with st.container(horizontal=True):
        otra = st.button("Otra rutina", type="primary", icon=":material/refresh:", width="stretch")
        cambiar = st.button("Cambiar opciones", icon=":material/tune:", width="stretch")
    if cambiar:
        st.session_state.rutina = None
        st.query_params.clear()
        st.rerun()
    if otra:
        generar(ejercicios, o, evitar=[item["id"] for item in st.session_state.rutina])
    if st.session_state.descartados:
        st.caption(f"Se descartaron {st.session_state.descartados} ejercicios que no estaban en la lista.")
    for posicion, item in enumerate(st.session_state.rutina, 1):
        tarjeta(posicion, item, ejercicios[item["id"]])


st.set_page_config(page_title="Rutinas", page_icon="🏋️")
pedir_contrasena()
try:
    ejercicios = cargar_ejercicios()
except (OSError, ValueError) as error:  # sin red, timeout, error HTTP o JSON corrupto
    st.error(f"No se pudo descargar el dataset de ejercicios: {error}")
    st.stop()
if "rutina" not in st.session_state:
    st.session_state.update(opciones=None, rutina=None, descartados=0)
    restaurar(ejercicios)
if st.session_state.rutina:
    pantalla_rutina(ejercicios)
else:
    pantalla_formulario(ejercicios)
