from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import date
import os
from dotenv import load_dotenv
from supabase import create_client, Client
from fastapi.staticfiles import StaticFiles
# Importamos el módulo de seguridad CORS
from fastapi.middleware.cors import CORSMiddleware

# 1. Cargamos las credenciales secretas
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 2. Inicializamos el cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Sistema de Bioseguridad Piscicultura")

# --- NUEVO: Configuración de CORS para desbloquear la conexión ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite que la web se conecte desde cualquier dirección (como Render)
    allow_credentials=True,
    allow_methods=["*"],  # Permite todos los métodos (POST, GET, etc.)
    allow_headers=["*"],  # Permite todas las cabeceras
)
# -----------------------------------------------------------------

class RegistroIngreso(BaseModel):
    fecha: date
    nombre_completo: str
    tipo_identificacion: str
    rut: str
    email: str

@app.post("/registro")
def crear_registro(datos: RegistroIngreso):
    try:
        nuevo_ingreso = datos.model_dump()
        nuevo_ingreso["fecha"] = str(nuevo_ingreso["fecha"])
        
        respuesta = supabase.table("registros_bioseguridad").insert(nuevo_ingreso).execute()
        
        return {
            "estado": "Guardado exitosamente en Supabase",
            "datos": respuesta.data
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error al conectar con Supabase: {str(e)}")

# 3. Montamos la carpeta 'static' en la raíz
app.mount("/", StaticFiles(directory="static", html=True), name="static")