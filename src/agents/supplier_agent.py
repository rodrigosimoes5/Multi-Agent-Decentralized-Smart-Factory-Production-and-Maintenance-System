from agents.base_agent import FactoryAgent
from spade.behaviour import CyclicBehaviour
from spade.message import Message

import json
import math
import time


class SupplierAgent(FactoryAgent):
    """Agente fornecedor de materiais (Supplier).

    O supplier participa num CNP de recursos com as máquinas
    (protocolo ``"supply-cnp"``) e delega o transporte em robots
    (protocolo ``"transport-cnp"``):

    - ``"supply-cnp"`` (Máquina - Supplier):
        * CFP:
            - A máquina envia pedido de materiais e posição.
            - O supplier verifica stock e estado (recharging ou não).
            - Se puder fornecer, responde com PROPOSE (custo, distância,
              materiais que consegue fornecer).
            - Caso contrário, responde REFUSE (``"no_stock"`` ou
              ``"recharging"``).
        * ACCEPT-PROPOSAL:
            - Reserva stock.
            - Inicia um CNP de transporte com robots (``"transport-cnp"``).
        * REJECT-PROPOSAL:
            - Apenas regista em log.
        * INFORM (vindo do supplier para a máquina):
            - Indica que os materiais foram entregues.

    - ``"transport-cnp"`` (Supplier - Robots):
        * O supplier atua como iniciador, enviando CFP para todos os robots.
        * Recolhe PROPOSE/REFUSE dos robots.
        * Escolhe o robot com menor custo e envia ACCEPT-PROPOSAL.
        * Quando recebe INFORM do robot vencedor, reencaminha INFORM final
          para a máquina, indicando que os materiais chegaram.

    Gestão de stock e recharging:

    - Mantém `max_stock_per_material` como valor máximo.
    - Quando detecta falta de stock num pedido:
        * Muda para estado `recharging`.
        * Regista o tick de início (`recharge_start_tick`).
        * Durante `recharge_duration_ticks` recusa todos os pedidos
          (REFUSE ``"recharging"``).
        * Ao fim desse período, repõe stock para o máximo em todos os
          materiais e volta a aceitar pedidos.


    """

    def __init__(
        self,
        jid,
        password,
        env=None,
        name="Supplier",
        stock_init=None,
        position=(0, 0),
        robots=None,
    ):
        """Inicializa o agente supplier.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Password associada ao JID.
            env: Ambiente/simulador partilhado, usado para obter o tempo
                em ticks e, se necessário, registar métricas.
            name (str): Nome lógico do supplier para efeitos de log.
            stock_init (dict[str, int] | None): Stock inicial por material.
                Se ``None``, é usado o stock máximo (`max_stock_per_material`)
                para os materiais padrão (``"material_1"`` e
                ``"material_2"``).
            position (tuple[float, float]): Posição (x, y) do supplier.
            robots (list[str] | None): Lista de JIDs de robots que podem
                executar transportes para este supplier. Se ``None``,
                é usada uma lista vazia.
        """
        super().__init__(jid, password, env=env)
        self.agent_name = name
        self.position = position

        self.max_stock_per_material = 40

        self.stock = stock_init or {
            "material_1": self.max_stock_per_material,
            "material_2": self.max_stock_per_material,
        }

        self.recharging = False
        self.recharge_start_tick = None
        self.recharge_duration_ticks = 5

        self.robots = robots or []

        self.pending_deliveries = {}

    async def setup(self):
        """Configura o supplier após o arranque.

        Este método:

        1. Chama o `setup` base (:class:`FactoryAgent`).
        2. Regista em log o stock inicial, a posição e a lista de robots.
        3. Adiciona o comportamento :class:`CNPBehaviour`, que trata tanto
           do CNP de recursos (``"supply-cnp"``) como do CNP de transporte
           (``"transport-cnp"`` do lado do supplier).
        """
        await super().setup()
        await self.log(
            f"(Supplier {self.agent_name}) stock={self.stock}, pos={self.position}, "
            f"robots={self.robots}"
        )
        self.add_behaviour(self.CNPBehaviour())

    class CNPBehaviour(CyclicBehaviour):
        """Comportamento CNP para materiais e transporte.

        Este comportamento lida com dois protocolos distintos:

        1) ``"supply-cnp"`` (Máquina - Supplier):
            - CFP:
                * Verifica se o supplier está em recharging.
                * Se estiver, recusa o pedido (REFUSE ``"recharging"``)
                  até terminar o período de recharging.
                * Se o período de recharging tiver terminado, repõe stock
                  e volta a aceitar pedidos.
                * Verifica se há stock suficiente para o pedido:
                    - Se não houver, entra (ou permanece) em estado
                      `recharging` e responde REFUSE ``"no_stock"``.
                    - Se houver, calcula custo (distância + 0.5 * quantidade
                      total) e responde PROPOSE.
            - ACCEPT-PROPOSAL:
                * Debita stock (reserva os materiais).
                * Regista a entrega pendente em `pending_deliveries`.
                * Inicia CNP de transporte com robots (protocolo
                  ``"transport-cnp"``).
            - REJECT-PROPOSAL:
                * Apenas regista em log.

        2) ``"transport-cnp"`` (Supplier - Robots):
            - INFORM:
                * Recebido do robot ao concluir o transporte.
                * Recupera a entrega pendente associada ao `thread_id`.
                * Envia INFORM final para a máquina, com
                  ``status="delivered"`` e materiais entregues.
        """

        async def run(self):
            """Processa uma mensagem CNP por iteração.

            Em cada iteração:

            1. Tenta receber uma mensagem com timeout de 1 segundo.
            2. Verifica o protocolo:
                - ``"supply-cnp"`` - trata CFP/ACCEPT/REJECT de pedidos de materiais.
                - ``"transport-cnp"`` - trata INFORM de robots (entrega concluída).
            3. Para ``"supply-cnp"``:
                - Em CFP:
                    * Aplica a lógica de recharging e verificação de stock.
                    * Devolve PROPOSE ou REFUSE.
                - Em ACCEPT-PROPOSAL:
                    * Reserva stock, regista entrega pendente e inicia CNP
                      de transporte com robots.
                - Em REJECT-PROPOSAL:
                    * Apenas escreve log.
            4. Para ``"transport-cnp"``:
                - Em INFORM:
                    * Conclui a entrega pendente e envia INFORM final para
                      a máquina.
            """
            agent = self.agent
            msg = await self.receive(timeout=1)
            if not msg:
                return

            protocol = msg.metadata.get("protocol")
            pf = msg.metadata.get("performative")
            thread_id = msg.metadata.get("thread")

            if protocol == "supply-cnp":
                try:
                    data = json.loads(msg.body) if msg.body else {}
                except Exception:
                    data = {}

                if pf == "cfp":
                    requested = data.get("materials", {})
                    machine_pos = data.get("machine_pos", [0, 0])

                    current_tick = agent.env.time if agent.env else None

                    if agent.recharging:
                        if (
                            current_tick is not None
                            and agent.recharge_start_tick is not None
                            and current_tick - agent.recharge_start_tick
                            >= agent.recharge_duration_ticks
                        ):
                            for mat in agent.stock.keys():
                                agent.stock[mat] = agent.max_stock_per_material

                            agent.recharging = False
                            agent.recharge_start_tick = None

                            await agent.log(
                                f"[SUPPLIER/{agent.agent_name}] Restock concluído. "
                                f"Stock reposto para {agent.stock}."
                            )
                        else:
                            rep = Message(to=str(msg.sender))
                            rep.set_metadata("protocol", "supply-cnp")
                            rep.set_metadata("performative", "refuse")
                            rep.set_metadata("thread", thread_id)
                            rep.body = json.dumps({"reason": "recharging"})
                            await self.send(rep)

                            await agent.log(
                                f"[SUPPLIER/{agent.agent_name}] REFUSE para {msg.sender} "
                                "(em recharging, a repor stock)"
                            )
                            return

                    has_stock = True
                    for mat, qty in requested.items():
                        if agent.stock.get(mat, 0) < qty:
                            has_stock = False
                            break

                    if not has_stock:
                        if not agent.recharging:
                            agent.recharging = True
                            agent.recharge_start_tick = current_tick
                            await agent.log(
                                f"[SUPPLIER/{agent.agent_name}] Stock insuficiente. "
                                f"Inicia período de recharging por "
                                f"{agent.recharge_duration_ticks} ticks "
                                f"(tick_inicio={agent.recharge_start_tick})."
                            )

                        rep = Message(to=str(msg.sender))
                        rep.set_metadata("protocol", "supply-cnp")
                        rep.set_metadata("performative", "refuse")
                        rep.set_metadata("thread", thread_id)
                        rep.body = json.dumps({"reason": "no_stock"})
                        await self.send(rep)

                        await agent.log(
                            f"[SUPPLIER/{agent.agent_name}] REFUSE para {msg.sender} "
                            "(stock insuficiente)"
                        )
                        return

                    dx = machine_pos[0] - agent.position[0]
                    dy = machine_pos[1] - agent.position[1]
                    distance = math.sqrt(dx * dx + dy * dy)
                    total_qty = sum(requested.values())
                    cost = distance + 0.5 * total_qty

                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "supply-cnp")
                    rep.set_metadata("performative", "propose")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps(
                        {
                            "cost": cost,
                            "distance": distance,
                            "can_supply": requested,
                        }
                    )
                    await self.send(rep)

                    await agent.log(
                        f"[SUPPLIER/{agent.agent_name}] PROPOSE para {msg.sender}: "
                        f"cost={cost:.2f}, dist={distance:.2f}, materials={requested}, "
                        f"stock_atual={agent.stock}"
                    )
                    return

                if pf == "accept-proposal":
                    requested = data.get("materials", {})
                    machine_pos = data.get("machine_pos", [0, 0])

                    for mat, qty in requested.items():
                        agent.stock[mat] = max(0, agent.stock.get(mat, 0) - qty)

                    machine_jid = str(msg.sender)
                    safe_thread = thread_id

                    agent.pending_deliveries[safe_thread] = {
                        "machine_jid": machine_jid,
                        "materials": requested,
                    }

                    await agent.log(
                        f"[SUPPLIER/{agent.agent_name}] ACCEPT-PROPOSAL de {machine_jid}. "
                        f"Materiais reservados={requested}. Lançando CNP-Transporte... "
                        f"Stock após reserva={agent.stock}"
                    )

                    if not agent.robots:
                        await agent.log(
                            "[SUPPLIER] ERRO: não há robots configurados. "
                            "Não é possível entregar materiais."
                        )
                        return

                    transport_payload = {
                        "kind": "materials",
                        "supplier_pos": list(agent.position),
                        "machine_pos": list(machine_pos),
                        "materials": requested,
                        "initiator_jid": str(agent.jid),
                    }

                    for r_jid in agent.robots:
                        m = Message(to=r_jid)
                        m.set_metadata("protocol", "transport-cnp")
                        m.set_metadata("performative", "cfp")
                        m.set_metadata("thread", safe_thread)
                        m.body = json.dumps(transport_payload)
                        await self.send(m)

                    proposals = []
                    deadline = time.time() + 3

                    while time.time() < deadline:
                        rep = await self.receive(timeout=0.5)
                        if not rep:
                            continue
                        if rep.metadata.get("protocol") != "transport-cnp":
                            continue
                        if rep.metadata.get("thread") != safe_thread:
                            continue

                        pf2 = rep.metadata.get("performative")
                        if pf2 == "propose":
                            try:
                                d2 = json.loads(rep.body) if rep.body else {}
                                cost = float(d2.get("cost", 9999))
                                proposals.append((str(rep.sender), cost))
                            except Exception:
                                continue
                        elif pf2 == "refuse":
                            await agent.log(
                                f"[SUPPLIER] Robot {rep.sender} recusou (busy/low_energy)."
                            )

                    if not proposals:
                        await agent.log(
                            f"[SUPPLIER] Nenhum robot aceitou transportar (thread={safe_thread})."
                        )
                        return

                    proposals.sort(key=lambda x: x[1])
                    winner_jid, winner_cost = proposals[0]

                    await agent.log(
                        f"[SUPPLIER] Robot vencedor={winner_jid} com cost={winner_cost:.2f}. "
                        "Enviando ACCEPT-PROPOSAL (transporte)."
                    )

                    for r_jid, _ in proposals[1:]:
                        rej = Message(to=r_jid)
                        rej.set_metadata("protocol", "transport-cnp")
                        rej.set_metadata("performative", "reject-proposal")
                        rej.set_metadata("thread", safe_thread)
                        rej.body = json.dumps({"reason": "not_selected"})
                        await self.send(rej)

                    acc = Message(to=winner_jid)
                    acc.set_metadata("protocol", "transport-cnp")
                    acc.set_metadata("performative", "accept-proposal")
                    acc.set_metadata("thread", safe_thread)
                    acc.body = json.dumps(transport_payload)
                    await self.send(acc)

                    return

                if pf == "reject-proposal":
                    await agent.log(
                        f"[SUPPLIER/{agent.agent_name}] REJECT recebido de {msg.sender}"
                    )
                    return

            if protocol == "transport-cnp":
                if pf == "inform":
                    info = agent.pending_deliveries.get(thread_id)
                    if not info:
                        await agent.log(
                            f"[SUPPLIER] INFORM de transporte com thread desconhecida: {thread_id}"
                        )
                        return

                    machine_jid = info["machine_jid"]
                    materials = info["materials"]

                    agent.pending_deliveries.pop(thread_id, None)

                    inf = Message(to=machine_jid)
                    inf.set_metadata("protocol", "supply-cnp")
                    inf.set_metadata("performative", "inform")
                    inf.set_metadata("thread", thread_id)
                    inf.body = json.dumps(
                        {
                            "status": "delivered",
                            "materials": materials,
                        }
                    )
                    await self.send(inf)

                    await agent.log(
                        f"[SUPPLIER/{agent.agent_name}] Transporte concluído. "
                        f"INFORM (delivered) enviado para {machine_jid}. "
                        f"Stock atual={agent.stock}"
                    )
                    return
