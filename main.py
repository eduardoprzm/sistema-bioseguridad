import os
import re
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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
    # Pydantic v2 antepone "Value error, " a los mensajes lanzados con raise ValueError
    mensaje = mensaje.replace("Value error, ", "")
    return JSONResponse(status_code=400, content={"detail": mensaje})


# ============================================================
# 🐟 CONTROL DE ALERTAS POR MOVIMIENTO ENTRE CENTROS
# ============================================================

def obtener_ultimo_registro_por_rut(rut: str) -> Optional[dict]:
    """Busca en Supabase el registro más reciente de este RUT (cualquier centro)."""
    respuesta = (
        supabase.table("registros_bioseguridad")
        .select("piscicultura, creado_en")
        .eq("rut", rut)
        .order("creado_en", desc=True)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return respuesta.data[0]
    return None


def evaluar_movimiento_entre_centros(rut: str, centro_actual: str) -> Optional[str]:
    """
    Revisa el historial real en Supabase para este RUT.
    Retorna un motivo de bloqueo (str) si corresponde restringir el acceso,
    o None si no hay riesgo de movimiento entre centros.
    """
    ultimo = obtener_ultimo_registro_por_rut(rut)
    if not ultimo:
        return None  # Sin historial previo, no hay riesgo de movimiento

    centro_anterior = ultimo["piscicultura"]

    # Mismo centro -> nunca se restringe por esta regla, aunque haya sido ayer
    if centro_anterior == centro_actual:
        return None

    try:
        fecha_anterior = datetime.fromisoformat(ultimo["creado_en"]).date()
    except (ValueError, TypeError):
        return None  # Si el timestamp viene en formato inesperado, no bloqueamos por esto

    dias_transcurridos = (date.today() - fecha_anterior).days

    if dias_transcurridos <= 2:
        return (
            f"Movimiento entre centros detectado: su último registro fue en "
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

    # 1. 🏭 VALIDACIÓN SEGÚN TIPO DE EMPRESA
    es_invermar = datos.empresa.lower() == "invermar"

    if not es_invermar:
        if not datos.centro_procedencia or not datos.centro_procedencia.strip():
            raise HTTPException(status_code=400, detail="El nombre del centro externo es obligatorio para contratistas.")
        if not datos.ultimo_ingreso_fecha:
            raise HTTPException(status_code=400, detail="La fecha de último ingreso es obligatoria para contratistas.")

    # 2. 🧠 REGLA DE CARENCIA SOBRE FECHA AUTODECLARADA (visitas a centros externos)
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

    # 3. 🐟 CONTROL AUTOMÁTICO DE MOVIMIENTO ENTRE CENTROS (según historial real en Supabase)
    if estado_acceso == "Acceso autorizado":
        motivo_movimiento = evaluar_movimiento_entre_centros(datos.rut, datos.centro)
        if motivo_movimiento:
            estado_acceso = "Acceso restringido"
            motivo_bloqueo = motivo_movimiento

    # 4. 🗄️ PREPARAR PAQUETE PARA SUPABASE
    fecha_registro = datos.ultimo_ingreso_fecha if datos.ultimo_ingreso_fecha else "2000-01-01"

    payload = {
        "piscicultura": datos.centro,
        "nombre_completo": datos.nombre_completo,
        "tipo_identificacion": datos.tipo_identificacion,
        "rut": datos.rut,
        "email": datos.email,
        "empresa": datos.empresa,
        "fecha": fecha_registro,
        "centro_procedencia": datos.centro_procedencia if not es_invermar else None,
        "estado_acceso": estado_acceso,
        "motivo_bloqueo": motivo_bloqueo,
    }

    try:
        supabase.table("registros_bioseguridad").insert(payload).execute()

        if estado_acceso == "Acceso restringido":
            raise HTTPException(
                status_code=403,
                detail=motivo_bloqueo or (
                    "Acceso restringido: Se prohíbe el acceso a las unidades productivas. "
                    "El tránsito se limita exclusivamente a las oficinas. Su visita será guiada."
                ),
            )

        return {
            "status": "success",
            "message": "Acceso autorizado. Su visita será guiada por el Jefe de Centro o por el encargado.",
        }

    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {str(e)}")