from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime
import os
from supabase import create_client, Client

app = FastAPI()

# --- CONFIGURACIÓN DE ARCHIVOS ESTÁTICOS Y RUTA RAÍZ ---
# Vincula la carpeta 'static' para que el servidor pueda leer el HTML, CSS o JS
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def servir_formulario():
    # Devuelve el archivo index.html cuando entras a la URL principal
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

# Estructura de datos alineada con la Nota Técnica de Invermar
class RegistroIngreso(BaseModel):
    centro: str
    nombre_completo: str
    tipo_identificacion: str
    rut: str
    email: str
    empresa: str
    ultimo_ingreso_fecha: Optional[str] = None    # "YYYY-MM-DD" o None (si es No recuerda)
    centro_procedencia: Optional[str] = None      # Texto o None (obligatorio si es externa)

@app.post("/api/registro")
async def registrar_ingreso(datos: RegistroIngreso):
    # Por defecto, el acceso es autorizado
    estado_acceso = "Acceso autorizado"
    motivo_bloqueo = None
    
    # 1. 🏭 VALIDACIÓN INICIAL: Lógica condicional según tipo de Empresa
    es_invermar = datos.empresa.lower() == "invermar"
    
    if not es_invermar:
        # Si es empresa externa, centro_procedencia y fecha son OBLIGATORIOS
        if not datos.centro_procedencia or not datos.centro_procedencia.strip():
            raise HTTPException(status_code=400, detail="El nombre del centro externo es obligatorio para contratistas.")
        if not datos.ultimo_ingreso_fecha:
            raise HTTPException(status_code=400, detail="La fecha de último ingreso es obligatoria para contratistas.")

    # 2. 🧠 MOTOR DE DECISIÓN SANITARIA (Regla de los 2 días)
    if datos.ultimo_ingreso_fecha:
        try:
            # Convertir texto a fecha para calcular la carencia
            fecha_visita = datetime.strptime(datos.ultimo_ingreso_fecha, "%Y-%m-%d").date()
            fecha_actual = date.today()
            
            dias_transcurridos = (fecha_actual - fecha_visita).days
            
            # REGLA: Si ingresó a un centro hace menos de 2 días -> Acceso Restringido
            if dias_transcurridos < 2:
                estado_acceso = "Acceso restringido"
                if es_invermar:
                    motivo_bloqueo = f"Incumple carencia de 2 días. Último ingreso a centro Invermar hace {dias_transcurridos} días."
                else:
                    motivo_bloqueo = f"Incumple carencia de 2 días. Provine de centro externo ({datos.centro_procedencia}) hace {dias_transcurridos} días."
                    
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD.")

    # 3. 🗄️ PREPARAR PAQUETE PARA SUPABASE (Alineado con las columnas de la BD)
    # Si la fecha viene vacía (No recuerda), usamos una fecha antigua para evitar el error de BD
    fecha_registro = datos.ultimo_ingreso_fecha if datos.ultimo_ingreso_fecha else "2000-01-01"

    payload = {
        "piscicultura": datos.centro,
        "nombre_completo": datos.nombre_completo,
        "tipo_identificacion": datos.tipo_identificacion,
        "rut": datos.rut,
        "email": datos.email,
        "empresa": datos.empresa,
        "fecha": fecha_registro,  # Columna corregida a 'fecha'
        "centro_procedencia": datos.centro_procedencia if not es_invermar else None,
        "estado_acceso": estado_acceso,
        "motivo_bloqueo": motivo_bloqueo
    }

    try:
        # Guardar el intento (tanto aprobado como restringido)
        supabase.table("registros_bioseguridad").insert(payload).execute()
        
        # 4. 🚨 RESPUESTA AL FRONTEND SEGÚN LA NORMATIVA
        if estado_acceso == "Acceso restringido":
            raise HTTPException(
                status_code=403, 
                detail="Acceso restringido: Se prohíbe el acceso a las unidades productivas. El tránsito se limita exclusivamente a las oficinas. Su visita será guiada."
            )
            
        return {
            "status": "success", 
            "message": "Acceso autorizado. Su visita será guiada por el Jefe de Centro o por el encargado."
        }
        
    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {str(e)}")