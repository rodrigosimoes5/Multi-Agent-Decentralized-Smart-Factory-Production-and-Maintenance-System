import asyncio
import spade
import matplotlib.pyplot as plt
import numpy as np
import sys
import matplotlib.font_manager as fm


plt.rcParams['font.sans-serif'] = ['Noto Color Emoji', 'Segoe UI Symbol', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

from environment import FactoryEnvironment
from agents.machine_agent import MachineAgent
from agents.supplier_agent import SupplierAgent
from agents.supervisor_agent import SupervisorAgent
from agents.robot_agent import RobotAgent
from agents.maintenance_agent import MaintenanceAgent


DOMAIN = "localhost"
PWD = "12345"


EMOJI_MAP = {
    'machine': '🏭',
    'robot': '🤖',
    'supplier': '📦',
    'maint': '🔧',
}


COLUMNS = {
    'supplier': 1.0,
    'robot': 3.0,
    'machine': 5.0,
    'maint': 7.0
}


def get_coords(count, y_pos, x_min, x_max):
    """Gera coordenadas (x, y) uniformemente distribuídas numa faixa horizontal.

    Esta função não está atualmente a ser usada no layout principal, mas
    representa uma forma genérica de distribuir agentes na horizontal.

    Args:
        count (int): Número de pontos a gerar.
        y_pos (float): Coordenada Y fixa para todos os pontos.
        x_min (float): Limite mínimo de X.
        x_max (float): Limite máximo de X.

    Returns:
        list[tuple[float, float]]: Lista de pares (x, y) espaçados
        uniformemente entre `x_min` e `x_max`. Se `count == 0`, devolve lista
        vazia.
    """
    if count == 0:
        return []
    x_values = np.linspace(x_min, x_max, count + 2)[1:-1]
    return [(round(x, 2), y_pos) for x in x_values]


def generate_agents_config(counts, domain, pwd, env):
    """Gera e configura os agentes da fábrica com base em contagens e layout.

    Esta função cria instâncias dos agentes:

    - Robots (:class:`RobotAgent`)
    - Maintenance crews (:class:`MaintenanceAgent`)
    - Suppliers (:class:`SupplierAgent`)
    - Machines (:class:`MachineAgent`)

    Cada tipo de agente é posicionado numa coluna fixa (`COLUMNS`), com
    espaçamento vertical constante. As máquinas recebem as listas de JIDs
    dos suppliers, maintenance crews e robots, para poderem usar CNPs.

    Args:
        counts (dict[str, int]): Número de agentes por tipo, por exemplo:
            ``{"machines": 2, "robots": 2, "maint": 2, "suppliers": 2}``.
        domain (str): Domínio XMPP (por exemplo, ``"localhost"``).
        pwd (str): Password a utilizar para todos os agentes.
        env (FactoryEnvironment): Ambiente partilhado da fábrica.

    Returns:
        tuple[list[spade.agent.Agent], list[str]]:
            - Lista com todos os agentes criados (máquinas, robots,
              suppliers, maintenance crews).
            - Lista de JIDs das máquinas, usada para instanciar o Supervisor.
    """
    N_MACHINES = counts['machines']
    N_ROBOTS = counts['robots']
    N_MAINT = counts['maint']
    N_SUPPLIERS = counts['suppliers']

    robots = []
    robots_jids = []
    machines = []
    machines_jids = []
    maintenance_crews = []
    maintenance_jids = []
    suppliers = []
    suppliers_jids = []

    Y_START = 1.0
    Y_STEP = 1.5

    robots_x = COLUMNS['robot']
    for i in range(N_ROBOTS):
        pos = (robots_x, Y_START + i * Y_STEP)
        name = f"R{i+1}"
        r = RobotAgent(
            f"robot{i+1}@{domain}", pwd, env=env, name=name,
            position=pos, speed=0.5
        )
        robots.append(r)
        robots_jids.append(str(r.jid))

    maint_x = COLUMNS['maint']
    for i in range(N_MAINT):
        pos = (maint_x, Y_START + i * Y_STEP)
        name = f"Crew-{i+1}"
        m = MaintenanceAgent(
            f"maint{i+1}@{domain}", pwd, env=env, name=name,
            position=pos, repair_time_range=(3, 7)
        )
        maintenance_crews.append(m)
        maintenance_jids.append(str(m.jid))

    
    suppliers_x = COLUMNS['supplier']
    if N_SUPPLIERS > 0:
        for i in range(N_SUPPLIERS):
            pos = (suppliers_x, Y_START + i * Y_STEP)
            name = f"S{i+1}"

            s = SupplierAgent(
                f"supplier{i+1}@{domain}", pwd, env=env, name=name,
                stock_init={"material_1": 40, "material_2": 40},
                position=pos, robots=robots_jids
            )
            suppliers.append(s)
            suppliers_jids.append(str(s.jid))

    
    machines_x = COLUMNS['machine']
    avg_reqs = {"material_1": 9, "material_2": 4.5}

    for i in range(N_MACHINES):
        pos = (machines_x, Y_START + i * Y_STEP)
        name = f"M{i+1}"
        m = MachineAgent(
            f"machine{i+1}@{domain}", pwd, env=env, name=name,
            position=pos, suppliers=suppliers_jids,
            material_requirements=avg_reqs, failure_rate=0.05,
            maintenance_crews=maintenance_jids, robots=robots_jids
        )
        machines.append(m)
        machines_jids.append(str(m.jid))

    all_agents = machines + robots + suppliers + maintenance_crews

    return all_agents, machines_jids



def draw_factory_state(env_data, ax_map, ax_metrics, current_counts):
    """Desenha o estado atual da fábrica (layout + métricas) com Matplotlib.

    A função produz duas vistas lado a lado:

    - `ax_map`: Mapa da fábrica com os agentes representados por emojis e
      caixas coloridas (verdes, vermelhas, etc.).
    - `ax_metrics`: Tabelas com métricas principais e uma legenda visual.

    Lógica de visualização:

    - Máquinas:
        * Caixa vermelha se falhada.
        * Caixa amarela se com job em execução.
        * Caixa cinzenta se idle.
    - Robots:
        * Caixa amarela se `is_busy` = True.
        * Caixa cinzenta caso contrário.
    - Maintenance crews:
        * Caixa amarela se em estado ``"busy"`` (a reparar).
        * Caixa cinzenta se ``"available"``.
        * Quando ocupada, a crew é desenhada junto da máquina alvo.
    - Suppliers:
        * Caixa vermelha se em `recharging` (sem stock).
        * Caixa cinzenta caso contrário.

    Args:
        env_data (dict): Snapshot do ambiente, tipicamente resultado de
            ``env.get_all_agents_status()``.
        ax_map (matplotlib.axes.Axes): Eixo onde é desenhado o mapa da fábrica.
        ax_metrics (matplotlib.axes.Axes): Eixo onde são desenhadas as métricas
            e a legenda.
        current_counts (dict[str, int]): Contagens atuais de agentes por tipo,
            usadas para ajustar dinamicamente os limites do gráfico.
    """
    ax_map.clear()

    
    Y_MAX_DYNAMIC = max(COLUMNS.values()) + max(current_counts.values()) * 1.5 + 2.0

    ax_map.set_xlim(0, 10)
    ax_map.set_ylim(-1, Y_MAX_DYNAMIC)
    ax_map.set_title(f'Layout da Fábrica (Tick: {env_data["time"]})', fontsize=12)
    ax_map.set_xlabel('Coordenada X')
    ax_map.set_ylabel('Coordenada Y')
    ax_map.grid(True)
    ax_map.invert_yaxis()

    agents = env_data['agents']

    for agent_type, agent_list in agents.items():
        if agent_type == 'supervisor':
            continue

        for a in agent_list:
            P_real = np.array(a['position'], dtype=float)
            P_draw = P_real.copy()
            status_tag = 'IDLE'

            
            if agent_type == 'machines':
                status_tag = 'FAIL' if a['is_failed'] else ('BUSY' if a['current_job_id'] else 'IDLE')

            elif agent_type == 'robots':
            
                status_tag = 'BUSY' if a['is_busy'] else 'IDLE'

            elif agent_type == 'maintenance_crews':
                
                status_tag = a['status'].upper()

            elif agent_type == 'suppliers':
            
                status_tag = 'RECHARGING_S' if a['recharging'] else 'IDLE'

            
            offset_dx, offset_dy = 0, 0

            if agent_type == 'machines':
                offset_dx, offset_dy = -0.3, 0
            elif agent_type == 'robots':
                offset_dx, offset_dy = 0.3, 0
            elif agent_type == 'maintenance_crews':
                if status_tag == 'BUSY' and a['repair_target']:
                    target_jid = a['repair_target']
                    target_machine = next(
                        (m for m in agents['machines'] if m['jid'] == target_jid),
                        None
                    )
                    if target_machine:
                        P_draw = np.array(target_machine['position'], dtype=float)
                        offset_dx, offset_dy = 0.5, 0.0
                else:
                    offset_dx, offset_dy = -0.5, 0
            elif agent_type == 'suppliers':
                offset_dx, offset_dy = 0, 0.5

            P_draw += [offset_dx, offset_dy]

            emoji = EMOJI_MAP.get(agent_type[:-1], '?')
            label = a['name']

            if status_tag in ('FAIL', 'RECHARGING_S'):
                box_color = 'red' 
            elif status_tag == 'BUSY':
                box_color = 'yellow'
            else:
                box_color = 'lightgray'

            ax_map.annotate(
                emoji,
                xy=P_draw,
                xytext=(P_draw[0], P_draw[1]),
                textcoords="data",
                ha='center', va='center',
                fontsize=20,
                bbox=dict(
                    boxstyle="square,pad=0.2",
                    fc=box_color,
                    alpha=0.9,
                    ec="black" if status_tag == 'FAIL' else "none"
                )
            )

            ax_map.annotate(
                label,
                xy=P_draw,
                xytext=(0, 25),
                textcoords="offset points",
                ha='center', fontsize=8, color='black'
            )

    
    ax_metrics.clear()
    ax_metrics.set_title(
        f'Métricas Principais (Tempo Total: {env_data["time"]} ticks)',
        fontsize=12
    )

    metrics = env_data['metrics']

    key_metrics = {
        'Jobs Completos': metrics['jobs_completed'],
        'Jobs Criados': metrics['jobs_created'],
        'Falhas Máquina': metrics['machine_failures'],
        'Downtime (Ticks)': metrics['downtime_ticks'],
        'Abastecimento OK': metrics['supply_requests_ok'],
        'Jobs Transferidos': metrics['jobs_transferred'],
    }

    col_labels = ['Métrica', 'Valor']
    table_data = [[label, value] for label, value in key_metrics.items()]

    table_metrics = ax_metrics.table(
        cellText=table_data,
        colLabels=col_labels,
        loc='upper center',
        cellLoc='left',
        bbox=[0.1, 0.65, 0.8, 0.3]
    )
    table_metrics.auto_set_font_size(False)
    table_metrics.set_fontsize(10)


    legend_data = [
        [EMOJI_MAP['machine'], 'Máquina de Produção'],
        [EMOJI_MAP['robot'], 'Robot de Transporte'],
        [EMOJI_MAP['supplier'], 'Supplier de Materiais'],
        ['?', 'Maintenance Crew'],
        ['Cor Amarela', 'Agente Ocupado (Trabalhando)'],
        ['Cor Cinzenta', 'Agente Livre/Disponível'],
        ['Cor Vermelha', 'Agente em Falha/Sem Stock (Problema)'],
    ]

    table_legend = ax_metrics.table(
        cellText=legend_data,
        colLabels=['Símbolo', 'Significado'],
        loc='lower center',
        cellLoc='left',
        bbox=[0.1, 0.15, 0.8, 0.4]
    )
    table_legend.auto_set_font_size(False)
    table_legend.set_fontsize(10)

    ax_metrics.axis('off')

    plt.draw()
    plt.show()


async def main():
    """Função principal da simulação SPADE com dashboard Matplotlib.

    Passos principais:

    1. Cria o :class:`FactoryEnvironment`.
    2. Pede ao utilizador os números de máquinas, robots, crews de manutenção
       e suppliers (com valores mínimos de 1).
    3. Gera os agentes com :func:`generate_agents_config`.
    4. Arranca todos os agentes SPADE (máquinas, robots, suppliers, manutenção).
    5. Arranca o Supervisor (:class:`SupervisorAgent`) configurado com os JIDs
       das máquinas e o intervalo entre jobs.
    6. Inicializa a figura Matplotlib em modo interativo.
    7. Executa um loop até `MAX_TICKS`:
        * Obtém o estado atual do ambiente via
          :meth:`FactoryEnvironment.get_all_agents_status`.
        * Desenha o estado com :func:`draw_factory_state`.
        * Avança o tempo global com :meth:`FactoryEnvironment.tick`.
    8. Em caso de erro, imprime a exceção.
    9. No final, fecha a figura, pára todos os agentes e imprime as métricas
       finais do ambiente.

    Esta função é passada a ``spade.run(main())`` para arrancar a simulação.
    """
    env = FactoryEnvironment()

    
    print("--- Configuração Inicial da Fábrica ---")
    try:
        counts = {
            'machines': max(1, int(input("Insira o número de Máquinas (Mín. 1): "))),
            'robots': max(1, int(input("Insira o número de Robôs (Mín. 1): "))),
            'maint': max(1, int(input("Insira o número de Crews de Manutenção (Mín. 1): "))),
            'suppliers': max(1, int(input("Insira o número de Suppliers (Mín. 1): "))),
        }
    except ValueError:
        print("\nInput inválido. Usando a configuração padrão (M=2, R=2, C=2, S=2).")
        counts = {'machines': 2, 'robots': 2, 'maint': 2, 'suppliers': 2}

    all_agents, machines_jids = generate_agents_config(counts, DOMAIN, PWD, env)

    for agent in all_agents:
        await agent.start(auto_register=True)

    supervisor = SupervisorAgent(
        f"supervisor@{DOMAIN}", PWD, env=env,
        machines=machines_jids, job_dispatch_every=5
    )
    await supervisor.start(auto_register=True)

    plt.ion()
    fig, (ax_map, ax_metrics) = plt.subplots(1, 2, figsize=(14, 7))
    fig.canvas.manager.set_window_title(
        f'Visualização da Fábrica (M:{counts["machines"]}, R:{counts["robots"]}, S:{counts["suppliers"]})'
    )

    
    MAX_TICKS = 500
    try:
        while env.time < MAX_TICKS:
            current_state = env.get_all_agents_status()

            draw_factory_state(current_state, ax_map, ax_metrics, counts)
            plt.pause(0.01)

            await env.tick()
            await asyncio.sleep(0.1)

    except Exception as e:
        print(f"\nErro durante a simulação: {e}")
    finally:
        plt.ioff()
        plt.close(fig)

        print("\n=== SIMULAÇÃO CONCLUÍDA ===")
        for agent in all_agents:
            if getattr(agent, 'is_started', True):
                await agent.stop()
        if getattr(supervisor, 'is_started', True):
            await supervisor.stop()

        print("\n=== MÉTRICAS FINAIS ===")
        for k, v in env.metrics.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    spade.run(main())
