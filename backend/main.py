from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import psycopg2
import os
from psycopg2.extras import RealDictCursor
from datetime import date
from fastapi import APIRouter, HTTPException

# Configuración de la APP
app = FastAPI(redirect_slashes=True)
router = APIRouter()

origins = [
    "https://inventario-laboratorio.onrender.com"
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    try:
        return psycopg2.connect(
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            cursor_factory=RealDictCursor
        )
    except Exception as e:
        print(f"Error conectando a la base de datos: {e}")
        return None


# --- MODELOS DE DATOS ---

class Material(BaseModel):
    tipo: str
    material: str 
    caracteristicas: str
    stock: float
    stock_minimo: float

class ItemPedido(BaseModel):
    material_id: int
    cantidad: int

class PedidoCreate(BaseModel):
    items: List[ItemPedido]

class AjusteStock(BaseModel):
    material_id: int
    tipo: str  # 'entrada' o 'salida'
    cantidad: float
    observacion: str
    lote_id: Optional[int] = None 
    es_reactivo: bool

class ReactivoRegistro(BaseModel):
    nombre: str
    categoria: str
    caracteristicas: str
    stock_minimo: float
    lote: str
    cantidad_frascos: int
    volumen_por_frasco: float
    fecha_vencimiento: date

# Nuevo modelo para añadir lotes a materiales existentes
class LoteAdicional(BaseModel):
    lote: str
    fecha_vencimiento: date
    cantidad_frascos: int
    volumen_por_frasco: float
    
class LotePayload(BaseModel):
    material_id: int
    lote: str
    fecha_vencimiento: date
    cantidad_frascos: int
    volumen_por_frasco: float
    
class UserRegister(BaseModel):
    username: str
    password: str
    rol: str

class UserLogin(BaseModel):
    username: str
    password: str
    
# --- ENDPOINTS DE DASHBOARD Y MATERIALES ---

@app.get("/dashboard/resumen")
def resumen_dashboard():
    conn = get_db_connection()
    if not conn: return {"alertas": 0, "pendientes": 0, "crisis": 0}
    cur = conn.cursor()
    try:
        # 1. Alertas de Stock Bajo (Global)
        cur.execute("SELECT COUNT(*) as total FROM materiales WHERE stock <= stock_minimo")
        alertas = cur.fetchone()['total']
        
        # 2. Pedidos Pendientes
        cur.execute("SELECT COUNT(*) as total FROM pedidos WHERE estado = 'pendiente'")
        pendientes = cur.fetchone()['total']

        # 3. Lógica de CRISIS (Regla: Solo si el ÚNICO stock disponible es el vencido)
        # Filtramos materiales que tienen stock > 0 pero 0 frascos con fecha vigente.
        cur.execute("""
            SELECT COUNT(*) as total FROM (
                SELECT m.id
                FROM materiales m
                JOIN frascos_reactivos f ON m.id = f.material_id
                WHERE f.volumen_actual > 0
                GROUP BY m.id
                HAVING COUNT(CASE WHEN f.fecha_vencimiento >= CURRENT_DATE THEN 1 END) = 0
            ) as subconsulta
        """)
        crisis = cur.fetchone()['total']
        
        return {
            "alertas": alertas, 
            "pendientes": pendientes, 
            "crisis": crisis
        }
    finally:
        cur.close()
        conn.close()

@app.get("/materiales")
def obtener_materiales():
    conn = get_db_connection()
    if not conn: return []
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM materiales ORDER BY material ASC")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

@app.post("/materiales")
def agregar_material(data: Material):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO materiales (tipo, material, caracteristicas, stock, stock_minimo) VALUES (%s, %s, %s, %s, %s)",
            (data.tipo, data.material, data.caracteristicas, data.stock, data.stock_minimo)
        )
        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# --- GESTIÓN DE LOTES ADICIONALES ---

@app.post("/materiales/{id}/lote")
def agregar_lote_existente(id: int, data: LoteAdicional):
    """Permite registrar varios lotes a un mismo material ya creado."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        total_ml_nuevo = data.cantidad_frascos * data.volumen_por_frasco
        
        # 1. Sumar al stock global del material
        cur.execute("UPDATE materiales SET stock = stock + %s WHERE id = %s", (total_ml_nuevo, id))
        
        # 2. Crear los nuevos frascos
        for _ in range(data.cantidad_frascos):
            cur.execute("""
                INSERT INTO frascos_reactivos (material_id, lote, fecha_vencimiento, volumen_inicial, volumen_actual, estado)
                VALUES (%s, %s, %s, %s, %s, 'activo')
            """, (id, data.lote, data.fecha_vencimiento, data.volumen_por_frasco, data.volumen_por_frasco))
        
        # 3. Registrar en historial
        cur.execute("""
            INSERT INTO historial_movimientos (material_id, tipo, cantidad, lote, observacion, es_reactivo)
            VALUES (%s, 'entrada', %s, %s, %s, True)
        """, (id, total_ml_nuevo, data.lote, f"Entrada de nuevo lote: {data.cantidad_frascos} frascos"))

        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# --- ENDPOINTS DE PEDIDOS ---

@app.post("/crear-pedido")
def crear_pedido(pedido: PedidoCreate, rol: str = "auxiliar"):
    # 1. Validación de seguridad en el servidor
    # Aunque el botón no aparezca en el front, esto evita ataques manuales
    if rol.lower() == "auxiliar":
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: El rol de Auxiliar no tiene permisos para crear pedidos."
        )

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Error de conexión a la base de datos")
        
    cur = conn.cursor()
    try:
        # 2. Insertar el encabezado del pedido
        # Agregué 'fecha' por si quieres mostrarla en el historial después
        cur.execute("INSERT INTO pedidos (estado, fecha) VALUES ('pendiente', CURRENT_TIMESTAMP) RETURNING id")
        pedido_id = cur.fetchone()['id']
        
        # 3. Insertar el detalle de los materiales solicitados
        for item in pedido.items:
            cur.execute(
                "INSERT INTO pedido_items (pedido_id, material_id, cantidad) VALUES (%s, %s, %s)",
                (pedido_id, item.material_id, item.cantidad)
            )
            
        conn.commit()
        return {"id": pedido_id, "status": "Pedido creado exitosamente"}
        
    except Exception as e:
        conn.rollback()
        print(f"Error al crear pedido: {e}")
        raise HTTPException(status_code=500, detail="Error interno al procesar el pedido")
    finally:
        cur.close()
        conn.close()

@app.get("/historial-pedidos")
def historial():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT p.id, p.fecha, p.estado, 
            string_agg(m.material || ' (' || pi.cantidad || ')', ', ') as detalle
            FROM pedidos p
            JOIN pedido_items pi ON p.id = pi.pedido_id
            JOIN materiales m ON pi.material_id = m.id
            GROUP BY p.id ORDER BY p.fecha DESC
        """)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()

@app.post("/pedido_entregado/{id}")
def marcar_entregado(id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE pedidos SET estado='entregado' WHERE id=%s", (id,))
        conn.commit()
        return {"status": "actualizado"}
    finally:
        cur.close()
        conn.close()

# --- GESTIÓN AVANZADA DE REACTIVOS ---

@app.post("/registrar-reactivo-completo")
def registrar_reactivo_completo(data: ReactivoRegistro):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        total_ml_inicial = data.cantidad_frascos * data.volumen_por_frasco
        cur.execute("""
            INSERT INTO materiales (tipo, material, caracteristicas, stock, stock_minimo)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (data.categoria, data.nombre, data.caracteristicas, total_ml_inicial, data.stock_minimo))
        material_id = cur.fetchone()['id']

        for _ in range(data.cantidad_frascos):
            cur.execute("""
                INSERT INTO frascos_reactivos (material_id, lote, fecha_vencimiento, volumen_inicial, volumen_actual, estado)
                VALUES (%s, %s, %s, %s, %s, 'activo')
            """, (material_id, data.lote, data.fecha_vencimiento, data.volumen_por_frasco, data.volumen_por_frasco))
        
        cur.execute("""
            INSERT INTO historial_movimientos (material_id, tipo, cantidad, lote, observacion, es_reactivo)
            VALUES (%s, 'entrada', %s, %s, %s, True)
        """, (material_id, total_ml_inicial, data.lote, f"Registro inicial de {data.cantidad_frascos} frascos"))

        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/material/{id}/lotes-disponibles")
def obtener_lotes(id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, lote, volumen_actual, fecha_vencimiento 
        FROM frascos_reactivos 
        WHERE material_id = %s AND volumen_actual > 0 AND estado = 'activo'
    """, (id,))
    res = cur.fetchall()
    cur.close()
    conn.close()
    return res

@app.post("/ajuste-stock")
def ajustar_stock(ajuste: AjusteStock):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        op = "+" if ajuste.tipo == 'entrada' else "-"
        cur.execute(f"UPDATE materiales SET stock = stock {op} %s WHERE id = %s", (ajuste.cantidad, ajuste.material_id))

        lote_nombre = "N/A"
        frasco_vacio = False
        if ajuste.es_reactivo and ajuste.lote_id:
            cur.execute(f"UPDATE frascos_reactivos SET volumen_actual = volumen_actual {op} %s WHERE id = %s RETURNING volumen_actual, lote", 
                        (ajuste.cantidad, ajuste.lote_id))
            res = cur.fetchone()
            if res:
                lote_nombre = res['lote']
                if res['volumen_actual'] <= 0:
                    cur.execute("UPDATE frascos_reactivos SET estado = 'agotado', volumen_actual = 0 WHERE id = %s", (ajuste.lote_id,))
                    frasco_vacio = True

        cur.execute("""
            INSERT INTO historial_movimientos (material_id, tipo, cantidad, lote, observacion, es_reactivo)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (ajuste.material_id, ajuste.tipo, ajuste.cantidad, lote_nombre, ajuste.observacion, ajuste.es_reactivo))

        conn.commit()
        return {"status": "ok", "eliminado": frasco_vacio}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/alertas-vencimiento")
def chequear_vencimiento():
    conn = get_db_connection()
    cur = conn.cursor()
    # Muestra todos los lotes que estén vencidos actualmente pero tengan stock
    cur.execute("""
        SELECT m.material, f.lote, f.fecha_vencimiento 
        FROM frascos_reactivos f
        JOIN materiales m ON f.material_id = m.id
        WHERE f.fecha_vencimiento < CURRENT_DATE AND f.estado = 'activo' AND f.volumen_actual > 0
    """)
    vencidos = cur.fetchall()
    cur.close()
    conn.close()
    return vencidos

# --- GESTIÓN DE NUEVOS LOTES (CORREGIDO PARA PSYCOPG2) ---

@app.post("/materiales/{material_id}/add-lote")
def agregar_nuevo_lote_final(material_id: int, payload: LotePayload):
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="No se pudo conectar a la base de datos")
    
    cur = conn.cursor()
    try:
        # 1. Verificar que el material exista
        cur.execute("SELECT id, material FROM materiales WHERE id = %s", (material_id,))
        material = cur.fetchone()
        if not material:
            raise HTTPException(status_code=404, detail="Material no encontrado")

        # 2. Calcular el volumen total que ingresa
        volumen_total_nuevo = payload.cantidad_frascos * payload.volumen_por_frasco

        # 3. Registrar cada frasco individualmente en frascos_reactivos
        for _ in range(payload.cantidad_frascos):
            cur.execute("""
                INSERT INTO frascos_reactivos 
                (material_id, lote, fecha_vencimiento, volumen_inicial, volumen_actual, estado)
                VALUES (%s, %s, %s, %s, %s, 'activo')
            """, (
                material_id, 
                payload.lote, 
                payload.fecha_vencimiento, 
                payload.volumen_por_frasco, 
                payload.volumen_por_frasco
            ))

        # 4. Actualizar el stock global en la tabla de materiales
        cur.execute("""
            UPDATE materiales 
            SET stock = stock + %s 
            WHERE id = %s
        """, (volumen_total_nuevo, material_id))

        # 5. Registrar en el historial de movimientos
        observacion = f"Ingreso de {payload.cantidad_frascos} frascos de {payload.volumen_por_frasco}ml"
        cur.execute("""
            INSERT INTO historial_movimientos 
            (material_id, tipo, cantidad, lote, observacion, es_reactivo)
            VALUES (%s, 'entrada', %s, %s, %s, True)
        """, (material_id, volumen_total_nuevo, payload.lote, observacion))

        conn.commit()
        return {
            "status": "ok", 
            "message": f"Se agregaron {volumen_total_nuevo} unidades al material {material['material']}"
        }

    except Exception as e:
        conn.rollback()
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()
        
@app.get("/materiales/stock-bajo")
def obtener_stock_bajo():
    conn = get_db_connection()
    if not conn:
        return []

    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT id, material, tipo, stock, stock_minimo
            FROM materiales
            WHERE stock <= stock_minimo
            ORDER BY stock ASC
        """)

        rows = cur.fetchall()

        materiales = []
        for r in rows:
            materiales.append({
                "id": r[0],
                "material": r[1],
                "tipo": r[2],
                "stock": r[3],
                "stock_minimo": r[4]
            })

        return materiales

    finally:
        cur.close()
        conn.close()

        
@app.get("/material/{id}/movimientos")
def obtener_historial_material(id: int):
    conn = get_db_connection()
    if not conn: 
        raise HTTPException(status_code=500, detail="Error de conexión")
    cur = conn.cursor()
    try:
        # 1. Primero obtenemos el nombre del material para el encabezado
        cur.execute("SELECT material FROM materiales WHERE id = %s", (id,))
        material = cur.fetchone()
        if not material:
            raise HTTPException(status_code=404, detail="Material no encontrado")
        
        # 2. Obtenemos todos los movimientos asociados a ese ID
        # Nota: Asegúrate de que tu tabla 'historial_movimientos' tenga la columna 'fecha'
        cur.execute("""
            SELECT tipo, cantidad, lote, observacion, es_reactivo, fecha
            FROM historial_movimientos 
            WHERE material_id = %s 
            ORDER BY fecha DESC
        """, (id,))
        movimientos = cur.fetchall()
        
        # 3. Retornamos la estructura que el HTML espera consumir
        return {
            "nombre": material['material'],
            "movimientos": movimientos
        }
    except Exception as e:
        print(f"Error al obtener historial: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()
        
@app.post("/auth/registrar")
def registrar_usuario(user: UserRegister):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO usuarios (username, password, rol) VALUES (%s, %s, %s)",
            (user.username, user.password, user.rol)
        )
        conn.commit()
        return {"status": "usuario creado"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail="El usuario ya existe o hay un error de datos")
    finally:
        cur.close()
        conn.close()

@app.post("/auth/login")
def login(user: UserLogin):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT rol FROM usuarios WHERE username = %s AND password = %s", (user.username, user.password))
    res = cur.fetchone()
    cur.close()
    conn.close()
    
    if res:
        return {"status": "ok", "rol": res['rol']}
    else:
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")