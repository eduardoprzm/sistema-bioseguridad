from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import date
import os
from dotenv import load_dotenv
from supabase import create_client, Client
# Importamos el módulo necesario para manejar archivos estáticos
from fastapi.staticfiles import StaticFiles

# 1. Cargamos las credenciales secretas del archivo .env
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 2. Inicializamos el cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Sistema de Bioseguridad Piscicultura")

class RegistroIngreso(BaseModel):
    fecha: date
    nombre_completo: str
    tipo_identificacion: str
    rut: str
    email: str

@app.post("/registro")
def crear_registro(datos: RegistroIngreso):
    try:
        # Convertimos los datos a un diccionario para Supabase
        nuevo_ingreso = datos.model_dump()
        
        # Convertimos la fecha a texto para evitar problemas de formato
        nuevo_ingreso["fecha"] = str(nuevo_ingreso["fecha"])
        
        # Insertamos los datos en la tabla de Supabase
        respuesta = supabase.table("registros_bioseguridad").insert(nuevo_ingreso).execute()
        
        return {
            "estado": "Guardado exitosamente en Supabase",
            "datos": respuesta.data
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error al conectar con Supabase: {str(e)}")

# 3. Montamos la carpeta 'static' para servir el archivo index.html en la raíz
app.mount("/", StaticFiles(directory="static", html=True), name="static")