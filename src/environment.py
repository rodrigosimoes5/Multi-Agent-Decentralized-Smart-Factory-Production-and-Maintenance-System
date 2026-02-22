import asyncio
import json


class FactoryEnvironment:
    """Ambiente partilhado da fábrica.

    Esta classe representa o contexto global da simulação:

    - `time`: contador discreto de ticks de simulação.
    - `metrics`: dicionário com contadores globais de desempenho.
    - `agents`: lista de instâncias dos agentes registados no ambiente.

    As métricas atualmente consideradas são:

    - Jobs / produção:
        * ``jobs_created``: número total de jobs criados pelo supervisor.
        * ``jobs_completed``: número de jobs concluídos com sucesso.
        * ``jobs_lost``: jobs cancelados ou perdidos.
    - Supply:
        * ``supply_requests_ok``: pedidos de materiais concluídos com sucesso.
        * ``supply_requests_failed``: pedidos de materiais que falharam/expiraram.
    - Falhas e manutenção:
        * ``machine_failures``: número total de falhas de máquinas.
        * ``repairs_started``: número de reparações iniciadas.
        * ``repairs_finished``: número de reparações concluídas.
        * ``downtime_ticks``: total de ticks em que máquinas estiveram falhadas.
    - Transferência de jobs:
        * ``jobs_transferred``: número de jobs transferidos entre máquinas
          com auxílio de robots.

    """

    def __init__(self):
        """Inicializa o ambiente com tempo zero e métricas a 0."""
        self.time = 0
        self.metrics = {
            
            "jobs_created": 0,
            "jobs_completed": 0,
            "jobs_lost": 0,

            
            "supply_requests_ok": 0,
            "supply_requests_failed": 0,
    
        
            "machine_failures": 0,
            "repairs_started": 0,
            "repairs_finished": 0,
            "downtime_ticks": 0,

        
            "jobs_transferred": 0,
        }
        self.agents = []

    def register_agent(self, agent):
        """Regista um agente no ambiente.

        Normalmente chamado a partir do método `setup()` de cada agente
        (via :class:`FactoryAgent`), de forma a que o ambiente possa:

        - Descobrir todos os agentes existentes.
        - Agregar estado (por exemplo, máquinas disponíveis).
        - Produzir um snapshot global para monitorização.

        Args:
            agent: Instância de um agente SPADE a registar neste ambiente.
        """
        self.agents.append(agent)

    def get_all_agents_status(self):
        """Recolhe o estado atual de todos os agentes registados.

        Produz uma estrutura de dados pronta a ser serializada em JSON,
        com a seguinte forma geral:

        .. code-block:: json

            {
              "metrics": { ... },
              "time": <int>,
              "agents": {
                "machines": [ ... ],
                "robots": [ ... ],
                "suppliers": [ ... ],
                "maintenance_crews": [ ... ],
                "supervisor": { ... }
              }
            }

        Para cada tipo de agente, são recolhidos campos relevantes:

        - **MachineAgent**:
            * name, jid, position
            * is_failed
            * queue_size, max_queue
            * current_job_id, current_step, ticks_remaining
        - **RobotAgent**:
            * name, jid, position
            * energy, max_energy, is_busy
        - **SupplierAgent**:
            * name, jid, position
            * stock, recharging
        - **MaintenanceAgent**:
            * name, jid, position
            * status (available/busy)
            * repair_target, ticks_remaining
        - **SupervisorAgent**:
            * jid
            * last_job_id

        Returns:
            dict: Estrutura de estado global da simulação, adequada para
            serialização com ``json.dumps`` ou para envio a um dashboard.
        """
        data = {
            "metrics": self.metrics,
            "time": self.time,
            "agents": {
                "machines": [],
                "robots": [],
                "suppliers": [],
                "maintenance_crews": [],
                "supervisor": {}
            }
        }

        for agent in self.agents:
            agent_type = agent.__class__.__name__
            
            if agent_type == "MachineAgent":
                current_job = getattr(agent, 'current_job', None)
                
                if current_job:
                    job_id = current_job.get('id')
                    job_step_idx = current_job.get('current_step_idx', 0)
                    pipeline = getattr(agent, 'pipeline_template', [])
                    current_step_name = pipeline[job_step_idx] if 0 <= job_step_idx < len(pipeline) else "UNKNOWN_STEP"
                    ticks_remaining = getattr(agent, 'current_job_ticks', 0)
                else:
                    job_id = None
                    current_step_name = None
                    ticks_remaining = 0
                
                state = {
                    "name": getattr(agent, 'agent_name', None),
                    "jid": str(agent.jid),
                    "position": getattr(agent, 'position', (0,0)),
                    "is_failed": getattr(agent, 'is_failed', False),
                    "queue_size": len(getattr(agent, 'job_queue', [])),
                    "max_queue": getattr(agent, 'max_queue', 15),
                    "current_job_id": job_id,
                    "current_step": current_step_name,
                    "ticks_remaining": ticks_remaining
                }
                data["agents"]["machines"].append(state)
            
            elif agent_type == "RobotAgent":
                state = {
                    "name": getattr(agent, 'agent_name', None),
                    "jid": str(agent.jid),
                    "position": getattr(agent, 'position', (0,0)),
                    "energy": getattr(agent, 'energy', 0),
                    "max_energy": getattr(agent, 'max_energy', 100),
                    "is_busy": getattr(agent, 'busy', False)
                }
                data["agents"]["robots"].append(state)
                
            elif agent_type == "SupplierAgent":
                state = {
                    "name": getattr(agent, 'agent_name', None),
                    "jid": str(agent.jid),
                    "position": getattr(agent, 'position', (0,0)),
                    "stock": getattr(agent, 'stock', {}),
                    "recharging": getattr(agent, 'recharging', False),
                }
                data["agents"]["suppliers"].append(state)
            
            
            elif agent_type == "MaintenanceAgent":
                current_repair = getattr(agent, 'current_repair', None)
                
                state = {
                    "name": getattr(agent, 'agent_name', None),
                    "jid": str(agent.jid),
                    "position": getattr(agent, 'position', (0,0)),
                    "status": getattr(agent, 'crew_status', 'available'),
                    "repair_target": current_repair.get('machine_jid') if current_repair else None,
                    "ticks_remaining": getattr(agent, 'repair_ticks_remaining', 0) if current_repair else 0
                }
                data["agents"]["maintenance_crews"].append(state)
            
            elif agent_type == "SupervisorAgent":
                data["agents"]["supervisor"] = {
                    "jid": str(agent.jid),
                    "last_job_id": getattr(agent, 'job_counter', 0),
                }

        return data

    async def tick(self):
        """Avança o tempo global de simulação uma unidade.

        Este método:

        - Incrementa o contador `time` em 1 tick.
        - Aguarda um pequeno intervalo (0.1 s) para simular o avanço do
          tempo físico/real na simulação.

        Pode ser chamado num loop principal do cenário, por exemplo:

        .. code-block:: python

            env = FactoryEnvironment()
            while True:
                await env.tick()
        """
        self.time += 1
        await asyncio.sleep(0.1)
