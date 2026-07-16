import os
import re
import secrets
from datetime import date, datetime
from typing import Optional
import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, model_validator
from supabase import create_client, Client

app = FastAPI()

# --- CONFIGURACIÓN DE ARCHIVOS ESTÁTICOS Y RUTA RAÍZ ---
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def servir_formulario():
    return FileResponse("static/index.html")


# --- CONFIGURACIÓN DE CORS PROTEGIDA ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://sistema-bioseguridad.onrender.com"],
    allow_credentials=True,
    allow_methods=["*", "POST"],
    allow_headers=["*"],
)

# Conexión a Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# URL base pública del sistema (para construir los enlaces de aprobación en el correo)
BASE_URL = os.getenv("BASE_URL", "https://sistema-bioseguridad.onrender.com")

# --- CONFIGURACIÓN DE CORREO (API HTTP de Brevo) ---
# Usamos la API HTTP (puerto 443) en vez de SMTP (puerto 587) porque los planes
# gratuitos de Render (y de muchos hostings) BLOQUEAN las conexiones salientes
# por los puertos típicos de SMTP para evitar spam. La API HTTP nunca tiene ese problema.
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
EMAIL_REMITENTE = os.getenv("SMTP_FROM", "notificaciones@invermar.cl")
NOMBRE_REMITENTE = os.getenv("NOMBRE_REMITENTE", "Sistema de Bioseguridad Invermar")

# --- MAPEO CENTRO -> CORREO DEL JEFE DE CENTRO ---
# Cada centro tiene su propia variable de entorno en Render. Mientras no configures
# la variable de un centro específico, cae al correo genérico EMAIL_JEFE_DEFAULT
# (útil ahora mismo para pruebas: pon tu propio correo en EMAIL_JEFE_DEFAULT y
# todos los centros te notificarán a ti).
EMAIL_JEFE_DEFAULT = os.getenv("EMAIL_JEFE_DEFAULT", "bioseguridad@invermar.cl")

JEFES_DE_CENTRO = {
    "Aucha": os.getenv("EMAIL_JEFE_AUCHA", EMAIL_JEFE_DEFAULT),
    "Lago verde": os.getenv("EMAIL_JEFE_LAGO_VERDE", EMAIL_JEFE_DEFAULT),
    "Traiguen I": os.getenv("EMAIL_JEFE_TRAIGUEN_I", EMAIL_JEFE_DEFAULT),
    "Traiguen II": os.getenv("EMAIL_JEFE_TRAIGUEN_II", EMAIL_JEFE_DEFAULT),
    "Auchac": os.getenv("EMAIL_JEFE_AUCHAC", EMAIL_JEFE_DEFAULT),
    "Chulin": os.getenv("EMAIL_JEFE_CHULIN", EMAIL_JEFE_DEFAULT),
    "Ester": os.getenv("EMAIL_JEFE_ESTER", EMAIL_JEFE_DEFAULT),
    "Mapue": os.getenv("EMAIL_JEFE_MAPUE", EMAIL_JEFE_DEFAULT),
    "Nayahue": os.getenv("EMAIL_JEFE_NAYAHUE", EMAIL_JEFE_DEFAULT),
    "Tepun": os.getenv("EMAIL_JEFE_TEPUN", EMAIL_JEFE_DEFAULT),
}


# ============================================================
# 🆔 VALIDACIÓN DE IDENTIDAD (RUT chileno / Pasaporte)
# ============================================================

def validar_rut_chileno(rut: str) -> bool:
    """Valida formato y dígito verificador de un RUT chileno.
    Acepta el rut ya limpio (sin puntos ni guion), ej: '12345678K'."""
    if len(rut) < 2:
        return False

    cuerpo, dv_ingresado = rut[:-1], rut[-1].upper()

    if not cuerpo.isdigit():
        return False

    suma = 0
    multiplo = 2
    for digito in reversed(cuerpo):
        suma += int(digito) * multiplo
        multiplo = multiplo + 1 if multiplo < 7 else 2

    resto = 11 - (suma % 11)
    if resto == 11:
        dv_calculado = "0"
    elif resto == 10:
        dv_calculado = "K"
    else:
        dv_calculado = str(resto)

    return dv_ingresado == dv_calculado


def validar_pasaporte(numero: str) -> bool:
    """Valida estructura básica de un pasaporte: alfanumérico, 6 a 15 caracteres."""
    return bool(re.fullmatch(r"[A-Z0-9]{6,15}", numero.upper()))


# ============================================================
# 📦 ESTRUCTURA DE DATOS + VALIDACIONES DE ENTRADA
# ============================================================

class RegistroIngreso(BaseModel):
    centro: str
    nombre_completo: str
    tipo_identificacion: str
    rut: str
    email: EmailStr                                # 📧 Valida estructura usuario@dominio.com
    zona_visita: Optional[str] = None              # "Oficina" o "Unidades Productivas" (informativo)
    empresa: str
    ultimo_ingreso_fecha: Optional[str] = None
    centro_procedencia: Optional[str] = None

    @model_validator(mode="after")
    def validar_documento_identidad(self):
        documento_limpio = self.rut.strip().upper().replace(".", "").replace("-", "")

        if not documento_limpio:
            raise ValueError("El campo RUT / Documento no puede estar vacío.")

        tipo = self.tipo_identificacion.strip().lower()

        if tipo == "rut":
            if not validar_rut_chileno(documento_limpio):
                raise ValueError(
                    "El RUT ingresado no es válido. Verifique el número y el dígito verificador."
                )
        elif tipo == "pasaporte":
            if not validar_pasaporte(documento_limpio):
                raise ValueError(
                    "El número de pasaporte no tiene un formato válido (debe tener entre 6 y 15 caracteres alfanuméricos)."
                )
        else:
            raise ValueError("Tipo de identificación no reconocido.")

        # Normalizamos el documento ya limpio para guardarlo consistente en la BD
        self.rut = documento_limpio
        return self


# Traduce los errores de validación de Pydantic a un mensaje simple y legible
@app.exception_handler(RequestValidationError)
async def manejar_errores_validacion(request: Request, exc: RequestValidationError):
    primer_error = exc.errors()[0]
    mensaje = primer_error.get("msg", "Datos inválidos.")
    mensaje = mensaje.replace("Value error, ", "")
    return JSONResponse(status_code=400, content={"detail": mensaje})


# ============================================================
# 📧 ENVÍO DE CORREO DE APROBACIÓN AL JEFE DE CENTRO
# ============================================================

def obtener_correo_jefe(centro: str) -> str:
    return JEFES_DE_CENTRO.get(centro, EMAIL_JEFE_DEFAULT)


def enviar_correo_generico(destino: str, asunto: str, cuerpo_html: str) -> None:
    """Función base para enviar cualquier correo vía la API HTTP de Brevo.
    Nunca lanza excepción hacia arriba: un fallo de envío no debe interrumpir
    el flujo de registro de ingreso."""
    if not BREVO_API_KEY:
        print(f"⚠️ BREVO_API_KEY no configurada: no se pudo enviar correo a {destino}.")
        return

    payload = {
        "sender": {"name": NOMBRE_REMITENTE, "email": EMAIL_REMITENTE},
        "to": [{"email": destino}],
        "subject": asunto,
        "htmlContent": cuerpo_html,
    }

    try:
        respuesta = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            json=payload,
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
            },
            timeout=10,
        )
        if respuesta.status_code >= 300:
            print(f"⚠️ Brevo respondió con error {respuesta.status_code}: {respuesta.text}")
        else:
            print(f"✅ Correo enviado a {destino}: {asunto}")
    except Exception as e:
        print(f"⚠️ Error enviando correo a {destino}: {e}")


def enviar_correo_aprobacion(registro_id: int, datos: "RegistroIngreso", motivo: str, token: str) -> None:
    """Envía un correo al jefe de centro con enlaces para aprobar o rechazar el ingreso."""
    destino = obtener_correo_jefe(datos.centro)
    link_aprobar = f"{BASE_URL}/api/aprobar/{registro_id}?token={token}"
    link_rechazar = f"{BASE_URL}/api/rechazar/{registro_id}?token={token}"

    cuerpo_html = f"""
    <div style="font-family: Arial, sans-serif; color:#1e293b;">
        <h2 style="color:#2563eb;">Solicitud de ingreso pendiente</h2>
        <p><b>Nombre:</b> {datos.nombre_completo}</p>
        <p><b>RUT/Documento:</b> {datos.rut}</p>
        <p><b>Empresa:</b> {datos.empresa}</p>
        <p><b>Centro solicitado:</b> {datos.centro}</p>
        <p><b>Motivo de la alerta:</b> {motivo}</p>
        <div style="margin-top:20px;">
            <a href="{link_aprobar}" style="background:#16a34a;color:white;padding:10px 20px;
               text-decoration:none;border-radius:8px;margin-right:10px;">✅ Aprobar</a>
            <a href="{link_rechazar}" style="background:#dc2626;color:white;padding:10px 20px;
               text-decoration:none;border-radius:8px;">❌ Rechazar</a>
        </div>
    </div>
    """
    enviar_correo_generico(
        destino,
        f"Bioseguridad: solicitud de ingreso pendiente - {datos.nombre_completo}",
        cuerpo_html,
    )


def notificar_jefe_registro(datos: "RegistroIngreso", estado_acceso: str, motivo: Optional[str] = None) -> None:
    """Notifica al jefe de centro de CUALQUIER ingreso registrado (autorizado o
    restringido), para que tenga visibilidad de quién entra a su centro,
    independientemente de si requirió aprobación manual o no."""
    destino = obtener_correo_jefe(datos.centro)

    color = "#16a34a" if estado_acceso == "Acceso autorizado" else "#dc2626"
    motivo_html = f"<p><b>Motivo:</b> {motivo}</p>" if motivo else ""

    cuerpo_html = f"""
    <div style="font-family: Arial, sans-serif; color:#1e293b;">
        <h2 style="color:{color};">Registro de ingreso: {estado_acceso}</h2>
        <p><b>Nombre:</b> {datos.nombre_completo}</p>
        <p><b>RUT/Documento:</b> {datos.rut}</p>
        <p><b>Empresa:</b> {datos.empresa}</p>
        <p><b>Centro:</b> {datos.centro}</p>
        <p><b>Zona a visitar:</b> {datos.zona_visita or "No especificada"}</p>
        {motivo_html}
    </div>
    """
    enviar_correo_generico(
        destino,
        f"Bioseguridad: {estado_acceso} - {datos.nombre_completo} ({datos.centro})",
        cuerpo_html,
    )


def notificar_visitante(destino: str, nombre: str, centro: str, estado_acceso: str, mensaje: str) -> None:
    """Envía al visitante una copia por correo del resultado de su declaración sanitaria."""
    color = "#16a34a" if estado_acceso == "Acceso autorizado" else (
        "#f59e0b" if estado_acceso == "Pendiente_Autorizacion" else "#dc2626"
    )
    titulo = {
        "Acceso autorizado": "Acceso autorizado",
        "Pendiente_Autorizacion": "Autorización pendiente",
        "Acceso restringido": "Acceso restringido",
    }.get(estado_acceso, estado_acceso)

    cuerpo_html = f"""
    <div style="font-family: Arial, sans-serif; color:#1e293b;">
        <h2 style="color:{color};">{titulo}</h2>
        <p>Hola {nombre},</p>
        <p>{mensaje}</p>
        <p style="color:#64748b; font-size:13px; margin-top:20px;">Instalación: {centro}</p>
    </div>
    """
    enviar_correo_generico(destino, f"Bioseguridad Invermar: {titulo} - {centro}", cuerpo_html)


# ============================================================
# 🐟 CONTROL DE ALERTAS POR MOVIMIENTO ENTRE CENTROS
# ============================================================

def obtener_ultimo_registro_autorizado(rut: str) -> Optional[dict]:
    """Busca en Supabase la última visita REALMENTE autorizada de este RUT.
    🔑 CLAVE DEL FIX: solo consideramos estado_acceso = 'Acceso autorizado'.
    Los intentos rechazados o pendientes NO cuentan como una visita real,
    así que nunca contaminan la comparación de 'último centro válido'."""
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("piscicultura, creado_en")
        .eq("rut", rut)
        .eq("estado_acceso", "Acceso autorizado")
        .order("creado_en", desc=True)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data[0]
    return None


def obtener_pendiente_sin_resolver(rut: str, centro: str) -> Optional[dict]:
    """Revisa si ya existe una solicitud 'Pendiente_Autorizacion' sin resolver
    para este mismo RUT y este mismo centro, para no duplicar correos."""
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("id, creado_en")
        .eq("rut", rut)
        .eq("piscicultura", centro)
        .eq("estado_acceso", "Pendiente_Autorizacion")
        .order("creado_en", desc=True)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data[0]
    return None


def evaluar_movimiento_entre_centros(rut: str, centro_actual: str) -> Optional[str]:
    """
    Revisa el historial REAL (solo visitas autorizadas) en Supabase para este RUT.
    Retorna un motivo de bloqueo (str) si corresponde restringir el acceso,
    o None si no hay riesgo de movimiento entre centros.
    """
    ultimo = obtener_ultimo_registro_autorizado(rut)
    if not ultimo:
        return None  # Sin historial autorizado previo, no hay riesgo de movimiento

    centro_anterior = ultimo["piscicultura"]

    # Mismo centro -> nunca se restringe por esta regla, aunque haya sido ayer
    if centro_anterior == centro_actual:
        return None

    try:
        fecha_anterior = datetime.fromisoformat(ultimo["creado_en"]).date()
    except (ValueError, TypeError):
        return None

    dias_transcurridos = (date.today() - fecha_anterior).days

    if dias_transcurridos <= 2:
        return (
            f"Movimiento entre centros detectado: su último ingreso autorizado fue en "
            f"'{centro_anterior}' hace {dias_transcurridos} día(s). Se requiere una "
            f"carencia mínima de 2 días antes de ingresar a '{centro_actual}'."
        )

    return None


# ============================================================
# 🚪 ENDPOINT PRINCIPAL DE REGISTRO
# ============================================================

@app.post("/api/registro")
async def registrar_ingreso(datos: RegistroIngreso):
    estado_acceso = "Acceso autorizado"
    motivo_bloqueo = None

    # 1. 🏭 IDENTIFICAR TIPO DE EMPRESA
    es_invermar = datos.empresa.lower() == "invermar"
    # Nota: centro_procedencia y ultimo_ingreso_fecha ya no son obligatorios para
    # contratistas externos, porque el formulario ahora permite marcar "No aplica"
    # cuando es su primera visita a un centro o piscicultura (sin historial previo).

    # 2. 🧠 REGLA DE CARENCIA SOBRE FECHA AUTODECLARADA (bloqueo directo, sin flujo de aprobación)
    if datos.ultimo_ingreso_fecha:
        try:
            fecha_visita = datetime.strptime(datos.ultimo_ingreso_fecha, "%Y-%m-%d").date()
            dias_transcurridos = (date.today() - fecha_visita).days

            if dias_transcurridos <= 2:
                estado_acceso = "Acceso restringido"
                if es_invermar:
                    motivo_bloqueo = f"Incumple carencia de 2 días. Último ingreso a centro Invermar hace {dias_transcurridos} días."
                else:
                    motivo_bloqueo = f"Incumple carencia de 2 días. Provine de centro externo ({datos.centro_procedencia}) hace {dias_transcurridos} días."
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD.")

    # 3. 🐟 CONTROL AUTOMÁTICO DE MOVIMIENTO ENTRE CENTROS -> FLUJO DE APROBACIÓN
    if estado_acceso == "Acceso autorizado":

        # 3a. ¿Ya hay una solicitud pendiente sin resolver para este RUT + centro?
        pendiente = obtener_pendiente_sin_resolver(datos.rut, datos.centro)
        if pendiente:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "pendiente",
                    "id": pendiente["id"],
                    "message": (
                        "Ya existe una solicitud de autorización pendiente para su ingreso a "
                        f"'{datos.centro}'. Por favor espere la confirmación del encargado de centro."
                    ),
                },
            )

        motivo_movimiento = evaluar_movimiento_entre_centros(datos.rut, datos.centro)
        if motivo_movimiento:
            # En vez de rechazar de inmediato, se guarda como Pendiente_Autorizacion
            fecha_registro = datos.ultimo_ingreso_fecha if datos.ultimo_ingreso_fecha else "2000-01-01"
            payload_pendiente = {
                "piscicultura": datos.centro,
                "nombre_completo": datos.nombre_completo,
                "tipo_identificacion": datos.tipo_identificacion,
                "rut": datos.rut,
                "email": datos.email,
                "zona_visita": datos.zona_visita,
                "empresa": datos.empresa,
                "fecha": fecha_registro,
                "centro_procedencia": datos.centro_procedencia,
                "estado_acceso": "Pendiente_Autorizacion",
                "motivo_bloqueo": motivo_movimiento,
                "token_aprobacion": secrets.token_urlsafe(32),
            }

            try:
                resultado = supabase.table("registros_bioseguridad").insert(payload_pendiente).execute()
                registro_id = resultado.data[0]["id"]
                token = payload_pendiente["token_aprobacion"]

                enviar_correo_aprobacion(registro_id, datos, motivo_movimiento, token)
                notificar_visitante(
                    datos.email, datos.nombre_completo, datos.centro, "Pendiente_Autorizacion",
                    "Recibimos su declaración sanitaria. Debido a un movimiento reciente entre "
                    "centros, su ingreso requiere autorización del encargado de centro. Le "
                    "avisaremos apenas se resuelva.",
                )

            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error de base de datos: {str(e)}")

            return JSONResponse(
                status_code=202,
                content={
                    "status": "pendiente",
                    "id": registro_id,
                    "message": (
                        "Su ingreso requiere autorización del encargado de centro debido a un "
                        "movimiento reciente entre centros. Se ha notificado al jefe de centro. "
                        "Por favor espere instrucciones del encargado de centro."
                    ),
                },
            )

    # 4. 🗄️ FLUJO NORMAL: GUARDAR RESULTADO (autorizado o restringido por carencia autodeclarada)
    fecha_registro = datos.ultimo_ingreso_fecha if datos.ultimo_ingreso_fecha else "2000-01-01"

    payload = {
        "piscicultura": datos.centro,
        "nombre_completo": datos.nombre_completo,
        "tipo_identificacion": datos.tipo_identificacion,
        "rut": datos.rut,
        "email": datos.email,
        "zona_visita": datos.zona_visita,
        "empresa": datos.empresa,
        "fecha": fecha_registro,
        "centro_procedencia": datos.centro_procedencia,
        "estado_acceso": estado_acceso,
        "motivo_bloqueo": motivo_bloqueo,
    }

    try:
        supabase.table("registros_bioseguridad").insert(payload).execute()

        notificar_jefe_registro(datos, estado_acceso, motivo_bloqueo)

        if estado_acceso == "Acceso restringido":
            notificar_visitante(
                datos.email, datos.nombre_completo, datos.centro, estado_acceso,
                motivo_bloqueo or "Su acceso a las unidades productivas ha sido restringido por bioseguridad.",
            )
            raise HTTPException(
                status_code=403,
                detail=motivo_bloqueo or (
                    "Acceso restringido: Se prohíbe el acceso a las unidades productivas. "
                    "El tránsito se limita exclusivamente a las oficinas. Su visita será guiada."
                ),
            )

        notificar_visitante(
            datos.email, datos.nombre_completo, datos.centro, estado_acceso,
            "Su ingreso ha sido autorizado. Su visita será guiada por el Jefe de Centro o por el encargado.",
        )

        return {
            "status": "success",
            "message": "Acceso autorizado. Su visita será guiada por el Jefe de Centro o por el encargado.",
        }

    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {str(e)}")


# ============================================================
# 🔑 RESOLUCIÓN: APROBAR / RECHAZAR DESDE EL CORREO
# ============================================================

def _pagina_html(titulo: str, mensaje: str, color: str) -> HTMLResponse:
    return HTMLResponse(f"""
    <html>
        <head><meta charset="UTF-8"><title>{titulo}</title></head>
        <body style="font-family: Arial, sans-serif; background:#f0f4f8; display:flex;
                     align-items:center; justify-content:center; height:100vh; margin:0;">
            <div style="background:white; padding:40px; border-radius:16px; box-shadow:0 4px 12px rgba(0,0,0,0.1);
                        text-align:center; max-width:400px;">
                <h1 style="color:{color};">{titulo}</h1>
                <p style="color:#1e293b;">{mensaje}</p>
            </div>
        </body>
    </html>
    """)


@app.get("/api/aprobar/{registro_id}")
async def aprobar_ingreso(registro_id: int, token: str):
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("id, estado_acceso, token_aprobacion, nombre_completo, piscicultura, email")
        .eq("id", registro_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        return _pagina_html("Solicitud no encontrada", "Este enlace no corresponde a ninguna solicitud.", "#dc2626")

    registro = respuesta.data[0]

    if registro["estado_acceso"] != "Pendiente_Autorizacion":
        return _pagina_html(
            "Solicitud ya resuelta",
            f"Esta solicitud ya fue procesada anteriormente (estado actual: {registro['estado_acceso']}).",
            "#f59e0b",
        )

    if registro["token_aprobacion"] != token:
        return _pagina_html("Enlace inválido", "El token de seguridad no coincide con esta solicitud.", "#dc2626")

    correo_encargado = obtener_correo_jefe(registro["piscicultura"])

    supabase.table("registros_bioseguridad").update(
        {"estado_acceso": "Acceso autorizado", "motivo_bloqueo": None, "autorizado_por": correo_encargado}
    ).eq("id", registro_id).execute()

    notificar_visitante(
        registro["email"], registro["nombre_completo"], registro["piscicultura"], "Acceso autorizado",
        "Su ingreso fue aprobado por el encargado de centro. Ya puede continuar con su visita.",
    )

    return _pagina_html(
        "✅ Ingreso aprobado",
        f"Se autorizó el ingreso de {registro['nombre_completo']} a '{registro['piscicultura']}'. "
        "No es necesario que la persona vuelva a escanear el código QR: el registro ya quedó "
        "actualizado en el sistema. Puede indicarle que continúe con su ingreso.",
        "#16a34a",
    )


@app.get("/api/rechazar/{registro_id}")
async def rechazar_ingreso(registro_id: int, token: str):
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("id, estado_acceso, token_aprobacion, nombre_completo, piscicultura, email")
        .eq("id", registro_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        return _pagina_html("Solicitud no encontrada", "Este enlace no corresponde a ninguna solicitud.", "#dc2626")

    registro = respuesta.data[0]

    if registro["estado_acceso"] != "Pendiente_Autorizacion":
        return _pagina_html(
            "Solicitud ya resuelta",
            f"Esta solicitud ya fue procesada anteriormente (estado actual: {registro['estado_acceso']}).",
            "#f59e0b",
        )

    if registro["token_aprobacion"] != token:
        return _pagina_html("Enlace inválido", "El token de seguridad no coincide con esta solicitud.", "#dc2626")

    correo_encargado = obtener_correo_jefe(registro["piscicultura"])

    supabase.table("registros_bioseguridad").update(
        {"estado_acceso": "Rechazado", "autorizado_por": correo_encargado}
    ).eq("id", registro_id).execute()

    notificar_visitante(
        registro["email"], registro["nombre_completo"], registro["piscicultura"], "Acceso restringido",
        "Su ingreso fue rechazado por el encargado de centro. Si tiene dudas, contacte a su anfitrión en Invermar.",
    )

    return _pagina_html(
        "❌ Ingreso rechazado",
        f"Se rechazó el ingreso de {registro['nombre_completo']} a '{registro['piscicultura']}'.",
        "#dc2626",
    )


# ============================================================
# 🔄 CONSULTA DE ESTADO (para que el visitante vea la resolución en vivo)
# ============================================================

@app.get("/api/estado/{registro_id}")
async def consultar_estado(registro_id: int):
    """Endpoint de solo lectura para que la pantalla de 'Autorización Pendiente'
    del visitante consulte periódicamente si el jefe de centro ya resolvió su solicitud."""
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("id, estado_acceso, motivo_bloqueo, piscicultura")
        .eq("id", registro_id)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Registro no encontrado.")

    registro = respuesta.data[0]

    mensajes = {
        "Pendiente_Autorizacion": "Su solicitud sigue pendiente de autorización.",
        "Acceso autorizado": "Su ingreso fue aprobado por el encargado de centro. Ya puede continuar con su visita.",
        "Rechazado": "Su ingreso fue rechazado por el encargado de centro.",
        "Acceso restringido": "Su ingreso fue restringido.",
    }

    return {
        "estado_acceso": registro["estado_acceso"],
        "message": mensajes.get(registro["estado_acceso"], registro["estado_acceso"]),
    }