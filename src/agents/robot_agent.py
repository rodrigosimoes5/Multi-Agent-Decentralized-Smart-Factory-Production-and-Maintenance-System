from agents.base_agent import FactoryAgent
from spade.behaviour import CyclicBehaviour
from spade.message import Message

import asyncio
import json
import math


def euclidean_distance(p1, p2):
    """Calcula a distância euclidiana entre dois pontos 2D.

    Args:
        p1 (Sequence[float]): Ponto inicial, como tuplo ou lista ``(x1, y1)``.
        p2 (Sequence[float]): Ponto final, como tuplo ou lista ``(x2, y2)``.

    Returns:
        float: Distância euclidiana entre `p1` e `p2`.
    """
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return math.sqrt(dx * dx + dy * dy)


class RobotAgent(FactoryAgent):
    """Robot de transporte com gestão de energia, responsável por transportar:

    - Materiais (dos suppliers para as máquinas).
    - Jobs (entre máquinas).

    O robot participa no protocolo ``"transport-cnp"``:

    A coordenação é feita via um protocolo CNP único, ``"transport-cnp"``,
em que o robot:

    1. Recebe CFP com a descrição do transporte (tipo, origem, destino, etc.).
    2. Se tiver energia suficiente e estiver livre, responde com PROPOSE,
    indicando custo, distância e energia necessária.
    3. Caso contrário, responde com REFUSE (por exemplo, ``"busy"``
    ou ``"low_energy"``).
    4. Quando recebe ACCEPT-PROPOSAL, executa o transporte:
    - Atualiza energia.
    - Simula tempo de viagem.
    - Atualiza a sua posição.
    - Envia INFORM com ``status="delivered"`` ao iniciador.
    5. Para jobs, pode ainda notificar a máquina de destino com o próprio job
    via protocolo ``"job-transfer"``.

    Inclui ainda um modelo simples de energia com recarga automática
    quando o robot está ocioso.

    """

    def __init__(
        self,
        jid,
        password,
        env=None,
        name="Robot",
        position=(0, 0),
        speed=1.0,
        max_energy=100,
        recharge_rate=5,
        energy_per_unit=1.0,
        low_energy_threshold=5,
    ):
        """Inicializa o agente robot de transporte.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Password associada ao JID.
            env: Ambiente/simulador partilhado, caso exista.
            name (str): Nome lógico do robot para efeitos de log.
            position (tuple[float, float]): Posição inicial do robot.
            speed (float): Fator utilizado para determinar o tempo de viagem
                a partir da distância total (``travel_time = dist * speed``).
            max_energy (int): Energia máxima do robot.
            recharge_rate (int): Percentagem de energia recarregada por tick
                quando o robot está ocioso.
            energy_per_unit (float): Energia consumida por unidade de distância.
            low_energy_threshold (int): Limiar abaixo do qual o robot tende
                a recusar novos transportes (por segurança).
        """
        super().__init__(jid, password, env=env)
        self.agent_name = name
        self.position = tuple(position)

        self.speed = speed

        self.max_energy = max_energy
        self.energy = max_energy
        self.recharge_rate = recharge_rate
        self.energy_per_unit = energy_per_unit
        self.low_energy_threshold = low_energy_threshold

        self.busy = False

    async def setup(self):
        """Configura o robot após o arranque.

        Este método:

        1. Chama o `setup` base (:class:`FactoryAgent`).
        2. Regista no log a posição inicial e a energia atual.
        3. Adiciona o comportamento :class:`TransportManagerBehaviour`,
           responsável pela recarga de energia, negociação CNP e execução
           dos transportes.
        """
        await super().setup()
        await self.log(
            f"[ROBOT] {self.agent_name} pronto. "
            f"pos={self.position}, energy={self.energy}%"
        )
        self.add_behaviour(self.TransportManagerBehaviour())

    class TransportManagerBehaviour(CyclicBehaviour):
        """Comportamento principal do robot para gestão de transportes.

        Responsabilidades:

       
        - Receber mensagens do protocolo ``"transport-cnp"``.
        - Para CFP:
            * Verificar se o robot está livre e se tem energia suficiente.
            * Calcular distância e energia necessária.
            * Responder com PROPOSE ou REFUSE.
        - Para ACCEPT-PROPOSAL:
            * Executar o transporte (simulação de viagem).
            * Atualizar energia e posição.
            * Enviar INFORM com ``status="delivered"``.
            * No caso de jobs, notificar também a máquina de destino
              via protocolo ``"job-transfer"``.
        - Para REJECT-PROPOSAL:
            * Apenas regista a rejeição no log.
        """

        async def run(self):
            """Executa um ciclo de gestão de energia e processamento de mensagens.

            Em cada iteração:

            
            1. Tenta receber uma mensagem com pequeno timeout.
            2. Se a mensagem pertencer ao protocolo ``"transport-cnp"``,
               processa-a de acordo com o seu `performative`:
                - ``"cfp"`` → devolve PROPOSE ou REFUSE.
                - ``"accept-proposal"`` → executa o transporte.
                - ``"reject-proposal"`` → apenas log.
            """
            agent = self.agent


            msg = await self.receive(timeout=0.3)
            if not msg:
                await asyncio.sleep(0.2)
                return

            if msg.metadata.get("protocol") != "transport-cnp":
                return

            pf = msg.metadata.get("performative")
            thread_id = msg.metadata.get("thread")

            try:
                task = json.loads(msg.body) if msg.body else {}
            except Exception:
                task = {}

            kind = task.get("kind", "materials")

            if kind == "materials":
                origin = task.get("supplier_pos", [0, 0])
                dest = task.get("machine_pos", [0, 0])
            else:
                origin = task.get("origin_pos", [0, 0])
                dest = task.get("dest_pos", [0, 0])

            d1 = euclidean_distance(agent.position, origin)
            d2 = euclidean_distance(origin, dest)
            total_dist = d1 + d2
            energy_needed = max(
                1, int(math.ceil(total_dist * agent.energy_per_unit))
            )

            if pf == "cfp":
                if agent.busy:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "transport-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "busy"})
                    await self.send(rep)
                    return

                if agent.energy < energy_needed or agent.energy <= agent.low_energy_threshold:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "transport-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "low_energy"})
                    await self.send(rep)

                    await agent.log(
                        f"[ROBOT] REFUSE (low_energy) para {msg.sender} "
                        f"(energy={agent.energy}%, needed={energy_needed}%)"
                    )
                    return

                total_qty = 0
                if kind == "materials":
                    total_qty = sum(task.get("materials", {}).values())

                cost = total_dist + 0.1 * total_qty

                rep = Message(to=str(msg.sender))
                rep.set_metadata("protocol", "transport-cnp")
                rep.set_metadata("performative", "propose")
                rep.set_metadata("thread", thread_id)
                rep.body = json.dumps(
                    {
                        "cost": cost,
                        "distance": total_dist,
                        "energy_needed": energy_needed,
                    }
                )
                await self.send(rep)

                await agent.log(
                    f"[ROBOT] PROPOSE para {msg.sender}: cost={cost:.2f}, "
                    f"dist={total_dist:.2f}, energy_needed={energy_needed} "
                    f"(energy atual={agent.energy}%)"
                )
                return

            if pf == "accept-proposal":
                agent.busy = True

                if agent.energy < energy_needed:
                    await agent.log(
                        f"[ROBOT] ERRO: energy insuficiente na hora do ACCEPT "
                        f"(energy={agent.energy}%, needed={energy_needed}%)."
                    )
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "transport-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "late_low_energy"})
                    await self.send(rep)
                    agent.busy = False
                    return

                prev_e = agent.energy
                agent.energy = max(0, agent.energy - energy_needed)

                await agent.log(
                    f"[ROBOT] ACCEPT-PROPOSAL recebido. "
                    f"Vai transportar kind={kind}, dist={total_dist:.2f}, "
                    f"energy {prev_e}% → {agent.energy}%"
                )

                travel_time = max(0.5, total_dist * agent.speed)
                await asyncio.sleep(travel_time)

                agent.position = tuple(dest)

                inf = Message(to=str(msg.sender))
                inf.set_metadata("protocol", "transport-cnp")
                inf.set_metadata("performative", "inform")
                inf.set_metadata("thread", thread_id)
                inf.body = json.dumps({"status": "delivered", "kind": kind})
                await self.send(inf)

                if kind == "job":
                    job = task.get("job", {})
                    dest_jid = task.get("destination_jid")

                    if dest_jid and job:
                        job_msg = Message(to=dest_jid)
                        job_msg.set_metadata("protocol", "job-transfer")
                        job_msg.set_metadata("performative", "inform")
                        job_msg.set_metadata("thread", thread_id)
                        job_msg.body = json.dumps(
                            {"status": "job_delivered", "job": job}
                        )
                        await self.send(job_msg)

                        await agent.log(
                            f"[ROBOT] Job {job.get('id')} entregue a {dest_jid} "
                            f"(via job-transfer)."
                        )

                await agent.log(
                    f"[ROBOT] Transporte concluído. "
                    f"thread={thread_id}, kind={kind}, pos_final={agent.position}"
                )

                agent.busy = False
                return

            if pf == "reject-proposal":
                await agent.log(
                    f"[ROBOT] REJECT recebido de {msg.sender} "
                    f"(thread={thread_id})."
                )
                return
