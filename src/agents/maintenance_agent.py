import asyncio
import json
import math

from spade.behaviour import CyclicBehaviour
from spade.message import Message

from agents.base_agent import FactoryAgent


class MaintenanceAgent(FactoryAgent):
    """Agente de manutenção (Maintenance Crew).

    Este agente representa uma equipa de manutenção que responde a pedidos
    de reparação de máquinas. Utiliza o protocolo ``maintenance-cnp``:

    - Recebe CFP com informação da máquina (JID e posição).
    - Se estiver disponível:
        * Calcula um custo com base na distância e no tempo de reparação.
        * Responde com PROPOSE.
    - Se for escolhida:
        * Recebe ACCEPT-PROPOSAL com `repair_time`.
        * Simula a reparação durante um certo número de ticks
          (:class:`RepairExecutor`).
        * Envia INFORM com ``status="repair_completed"`` para a máquina.

    """

    def __init__(
        self,
        jid,
        password,
        env=None,
        name="MaintenanceCrew",
        position=(0, 0),
        repair_time_range=(3, 8),
    ):
        """Inicializa o agente de manutenção.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Password associada ao JID.
            env: Referência ao ambiente/simulador, usada para atualizar métricas,
                como o número de reparações concluídas.
            name (str): Nome da equipa de manutenção para efeitos de log.
            position (tuple[float, float]): Posição inicial (x, y) da equipa.
            repair_time_range (tuple[int, int]): Intervalo (mín, máx) em ticks
                para a duração de reparações.
        """
        super().__init__(jid, password, env=env)
        self.agent_name = name
        self.position = position
        self.repair_time_range = repair_time_range

        self.crew_status = "available"
        self.current_repair = None
        self.repair_ticks_remaining = 0

    async def setup(self):
        """Configura o agente após o arranque.

        Este método:

        1. Chama o setup base (:class:`FactoryAgent`) para registo no ambiente.
        2. Escreve uma mensagem de log com a posição e estado inicial da
           equipa de manutenção.
        3. Adiciona dois comportamentos cíclicos:
            - :class:`MaintenanceParticipant` para participação no CNP
              de manutenção;
            - :class:`RepairExecutor` para simular a execução da reparação
              ao longo do tempo.
        """
        await super().setup()
        await self.log(
            f"[MAINT] {self.agent_name} pronto. "
            f"pos={self.position}, status={self.crew_status}"
        )
        self.add_behaviour(self.MaintenanceParticipant())
        self.add_behaviour(self.RepairExecutor())

    
    class MaintenanceParticipant(CyclicBehaviour):
        """Comportamento CNP para responder a pedidos de manutenção.

        Este comportamento trata da parte de negociação:

        - Recebe mensagens com o protocolo ``"maintenance-cnp"``.
        - Para mensagens com performative ``"cfp"``:
            * Se a equipa estiver disponível → calcula um custo e faz PROPOSE.
            * Caso contrário → responde REFUSE, indicando o estado atual.
        - Para mensagens com performative ``"accept-proposal"``:
            * Regista a reparação atual e define o número de ticks restantes.
            * Atualiza o estado da equipa para ``"busy"``.
            * Opcionalmente envia um INFORM com ``"repair_started"``.
        - Para mensagens com performative ``"reject-proposal"``:
            * Apenas regista a rejeição no log.
        """

        async def run(self):
            """Processa uma mensagem do protocolo de manutenção.

            Este método é chamado ciclicamente pelo motor de comportamentos
            do SPADE. Em cada iteração:

            1. Tenta ler uma mensagem com timeout curto.
            2. Se não houver mensagem ou se não for do protocolo
               ``"maintenance-cnp"``, termina a iteração.
            3. Com base no performative (``cfp``, ``accept-proposal``,
               ``reject-proposal``), executa a lógica correspondente descrita
               em :class:`MaintenanceParticipant`.

            Protocolo de mensagens:
                - CFP:
                    * metadata:
                        - ``protocol`` = ``"maintenance-cnp"``
                        - ``performative`` = ``"cfp"``
                        - ``thread`` = ID do leilão/negociação
                    * body (JSON):
                        - ``machine_jid`` (str)
                        - ``machine_pos`` (list[float, float])
                - PROPOSE:
                    * metadata:
                        - ``protocol`` = ``"maintenance-cnp"``
                        - ``performative`` = ``"propose"``
                        - ``thread`` = mesmo ID da CFP
                    * body (JSON):
                        - ``crew`` (str): nome da equipa.
                        - ``cost`` (float): custo estimado.
                        - ``repair_time`` (int): duração estimada da reparação.
                        - ``distance`` (float): distância à máquina.
                - REFUSE:
                    * metadata:
                        - ``protocol`` = ``"maintenance-cnp"``
                        - ``performative`` = ``"refuse"``
                        - ``thread`` = mesmo ID da CFP
                    * body (JSON, opcional):
                        - ``reason`` (str): motivo da recusa (por ex., ``"busy"``).
                - ACCEPT-PROPOSAL / REJECT-PROPOSAL:
                    * metadata:
                        - ``protocol`` = ``"maintenance-cnp"``
                        - ``performative`` = ``"accept-proposal"`` ou
                          ``"reject-proposal"``
                        - ``thread`` = mesmo ID da CFP
                    * body:
                        - No caso de ``accept-proposal``: inclui informação
                          da máquina e ``repair_time``.
            """
            agent = self.agent
            msg = await self.receive(timeout=0.5)
            if not msg:
                return

            if msg.metadata.get("protocol") != "maintenance-cnp":
                return

            pf = msg.metadata.get("performative")
            thread_id = msg.metadata.get("thread")

            try:
                data = json.loads(msg.body) if msg.body else {}
            except Exception:
                data = {}

            if pf == "cfp":
                machine_jid = data.get("machine_jid")
                machine_pos = data.get("machine_pos", [0, 0])

                if agent.crew_status != "available":
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "maintenance-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": agent.crew_status})
                    await self.send(rep)

                    await agent.log(
                        f"[MAINT-CNP] REFUSE para {machine_jid} (status={agent.crew_status})"
                    )
                    return

                dx = machine_pos[0] - agent.position[0]
                dy = machine_pos[1] - agent.position[1]
                distance = math.sqrt(dx * dx + dy * dy)

                repair_time = random_int_in_range(agent.repair_time_range)
                cost = distance + repair_time

                rep = Message(to=str(msg.sender))
                rep.set_metadata("protocol", "maintenance-cnp")
                rep.set_metadata("performative", "propose")
                rep.set_metadata("thread", thread_id)
                rep.body = json.dumps(
                    {
                        "crew": agent.agent_name,
                        "cost": cost,
                        "repair_time": repair_time,
                        "distance": distance,
                    }
                )
                await self.send(rep)

                await agent.log(
                    f"[MAINT-CNP] PROPOSE para {machine_jid}: "
                    f"cost={cost:.2f}, repair_time={repair_time}, dist={distance:.2f}"
                )
                return

            if pf == "accept-proposal":
                machine_jid = data.get("machine_jid")
                machine_pos = data.get("machine_pos", [0, 0])
                repair_time = int(data.get("repair_time", 5))

                agent.current_repair = {
                    "machine_jid": machine_jid,
                    "machine_pos": machine_pos,
                    "thread": thread_id,
                }
                agent.repair_ticks_remaining = repair_time
                agent.crew_status = "busy"

                await agent.log(
                    f"[MAINT-CNP] ACCEPT de {machine_jid}. "
                    f"Iniciar reparação ({repair_time} ticks)."
                )

                inf = Message(to=machine_jid)
                inf.set_metadata("protocol", "maintenance-cnp")
                inf.set_metadata("performative", "inform")
                inf.set_metadata("thread", thread_id)
                inf.body = json.dumps({"status": "repair_started"})
                await self.send(inf)
                return

            # REJECT-PROPOSAL → apenas logar
            if pf == "reject-proposal":
                await agent.log("[MAINT-CNP] REJECT recebido.")
                return

    class RepairExecutor(CyclicBehaviour):
        """Comportamento responsável por simular a execução da reparação.

        Este comportamento:

        - Verifica se existe uma reparação em curso (`current_repair`).
        - Se não houver, marca a equipa como ``"available"`` e fica em espera.
        - Se houver, decrementa `repair_ticks_remaining` a cada execução,
          simulando a passagem de um tick de reparação.
        - Quando o número de ticks chegar a zero:
            * Envia uma mensagem INFORM com ``"repair_completed"`` para a
              máquina correspondente.
            * Atualiza o estado da equipa para ``"available"``.
            * Incrementa a métrica global ``repairs_finished`` no ambiente.
        """

        async def run(self):
            """Atualiza o estado da reparação em curso.

            Este método é chamado ciclicamente, representando o avanço de
            um tick de simulação para a reparação:

            - Se `current_repair` for ``None``:
                * Marca a equipa como disponível.
                * Aguarda um curto intervalo antes da próxima iteração.
            - Se `current_repair` não for ``None``:
                * Decrementa `repair_ticks_remaining`.
                * Regista no log o número de ticks restantes.
                * Quando `repair_ticks_remaining` chega a 0 ou menos:
                    - Envia INFORM para a máquina com
                      ``status="repair_completed"``.
                    - Limpa `current_repair` e volta a marcar a equipa como
                      disponível.
                    - Atualiza a métrica `repairs_finished` no ambiente, se
                      este existir.
            """
            agent = self.agent

            if agent.current_repair is None:
                agent.crew_status = "available"
                await asyncio.sleep(0.5)
                return

            if agent.repair_ticks_remaining > 0:
                agent.repair_ticks_remaining -= 1
                await agent.log(
                    f"[REPAIR] Reparando {agent.current_repair['machine_jid']}: "
                    f"{agent.repair_ticks_remaining} ticks restantes."
                )

            if agent.repair_ticks_remaining <= 0:
                machine_jid = agent.current_repair["machine_jid"]
                thread_id = agent.current_repair["thread"]

                inf = Message(to=machine_jid)
                inf.set_metadata("protocol", "maintenance-cnp")
                inf.set_metadata("performative", "inform")
                inf.set_metadata("thread", thread_id)
                inf.body = json.dumps({"status": "repair_completed"})
                await self.send(inf)

                await agent.log(
                    f"[REPAIR] Reparação concluída para {machine_jid}. "
                    "INFORM 'repair_completed' enviado."
                )

                agent.current_repair = None
                agent.crew_status = "available"

                if agent.env:
                    agent.env.metrics["repairs_finished"] += 1

            await asyncio.sleep(1)


def random_int_in_range(rng):
    """Gera um inteiro aleatório dentro de um intervalo.

    Função auxiliar simples que devolve um número inteiro aleatório dentro
    do intervalo dado por `rng`, usando ``random.randint``.

    Args:
        rng (tuple[int, int]): Intervalo (mín, máx) no qual gerar o número
            inteiro aleatório.

    Returns:
        int: Número inteiro aleatório no intervalo `[rng[0], rng[1]]`.
    """
    import random
    return random.randint(rng[0], rng[1])
