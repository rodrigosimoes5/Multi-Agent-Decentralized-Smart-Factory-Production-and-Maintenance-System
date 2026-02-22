from agents.base_agent import FactoryAgent
from spade.behaviour import CyclicBehaviour
from spade.message import Message

import asyncio
import json
import random


class MachineAgent(FactoryAgent):
    """Agente de máquina de produção.

    Este módulo define o :class:`MachineAgent`, responsável por:

    - Negociar jobs com o Supervisor via protocolo ``"job-dispatch"`` (CNP).
    - Obter materiais de suppliers via protocolo ``"supply-cnp"`` (CNP de recursos).
    - Executar um pipeline de produção em vários steps (step0–step3).
    - Simular falhas com certa probabilidade e pedir manutenção às crews via
    protocolo ``"maintenance-cnp"``.
    - Transferir jobs para outras máquinas em caso de falha, usando:
        * CNP de transferência entre máquinas (``"transfer-cnp"``);
        * Robots de transporte via protocolo ``"transport-cnp"`` para levar
        o job fisicamente até à máquina destino;
        * Protocolo ``"job-transfer"`` do lado da máquina destino para receber
        o job entregue pelo robot.

    A máquina trabalha com uma fila de jobs (`job_queue`) e um job atual
    (`current_job`) que percorre um pipeline de etapas com tempos fixos.
    """

    def __init__(
        self,
        jid,
        password,
        env=None,
        name="Machine",
        position=(0, 0),
        suppliers=None,
        material_requirements=None,
        max_queue=15,
        failure_rate=0.10,
        maintenance_crews=None,
        robots=None,
    ):
        """Inicializa o agente de máquina.

        Args:
            jid (str): JID XMPP do agente, por exemplo ``"machine1@localhost"``.
            password (str): Password associada ao JID.
            env: Ambiente/simulador partilhado, usado para:
                - Aceder ao tempo de simulação;
                - Registar métricas (jobs, falhas, manutenção, etc.).
            name (str): Nome lógico da máquina para efeitos de log.
            position (tuple[float, float]): Posição (x, y) da máquina.
            suppliers (list[str] | None): Lista de JIDs dos suppliers.
            material_requirements (dict[str, int] | None): Materiais
                necessários por job. Se ``None``, assume
                ``{"material_1": 10, "material_2": 5}``.
            max_queue (int): Capacidade máxima da fila de jobs.
            failure_rate (float): Probabilidade de falha por tick em etapas
                de processamento (step1/2/3).
            maintenance_crews (list[str] | None): JIDs das equipas de
                manutenção disponíveis.
            robots (list[str] | None): JIDs dos robots que podem transportar jobs.
        """
        super().__init__(jid, password, env=env)
        self.agent_name = name
        self.position = position

        self.job_queue = []
        self.current_job = None
        self.current_job_ticks = 0

        self.max_queue = max_queue

        self.suppliers = suppliers or []

        self.material_requirements = material_requirements or {
            "material_1": 10,
            "material_2": 5,
        }

        self.pipeline_template = ["step0", "step1", "step2", "step3"]
        self.step_durations = {
            "step0": 1,
            "step1": 3,
            "step2": 4,
            "step3": 2,
        }

        self.failure_rate = failure_rate
        self.is_failed = False
        self.maintenance_crews = maintenance_crews or []
        self.robots = robots or []
        self.maintenance_requested = False
        self.repair_in_progress = False

        self.job_transfer_done = False

        self.is_machine = True

    async def setup(self):
        """Configura a máquina após o arranque.

        Este método:

        1. Chama o `setup` base (:class:`FactoryAgent`) para registo no ambiente.
        2. Regista em log a posição, a capacidade da fila, os suppliers e o
           pipeline.
        3. Adiciona vários comportamentos que implementam:
            - CNP com Supervisor (``JobDispatchParticipant``),
            - Execução de jobs (``JobExecutor``),
            - Pedido de manutenção (``MaintenanceRequester``),
            - Receção de respostas de manutenção (``MaintenanceResponseHandler``),
            - CNP de transferência (``JobTransferInitiator``, ``JobTransferParticipant``),
            - Receção de jobs transferidos (``JobTransferReceiver``).
        """
        await super().setup()
        await self.log(
            f"Machine {self.agent_name} pronta. pos={self.position}, "
            f"max_queue={self.max_queue}, suppliers={self.suppliers}, "
            f"pipeline={self.pipeline_template}, failure_rate={self.failure_rate}"
        )

        self.add_behaviour(self.JobDispatchParticipant())
        self.add_behaviour(self.JobExecutor())
        self.add_behaviour(self.MaintenanceRequester())
        self.add_behaviour(self.MaintenanceResponseHandler())
        self.add_behaviour(self.JobTransferInitiator())
        self.add_behaviour(self.JobTransferParticipant())
        self.add_behaviour(self.JobTransferReceiver())


    async def handle_failure(self, job):
        """Marca a máquina como falhada e regista a ocorrência de falha.

        Ao ser chamada, esta função:

        - Marca a máquina como falhada (`is_failed = True`).
        - Reinicia flags de manutenção e transferência de job.
        - Incrementa a métrica ``env.metrics["machine_failures"]``.
        - Regista no log a etapa do pipeline onde ocorreu a falha.

        Args:
            job (dict): Dicionário com o estado do job atual, incluindo
                pipeline e índice da etapa atual.
        """
        if self.is_failed:
            return

        self.is_failed = True
        self.maintenance_requested = False
        self.job_transfer_done = False

        if self.env:
            self.env.metrics["machine_failures"] += 1

        step = job["pipeline"][job["current_step_idx"]]
        await self.log(
            f"[FAILURE] {self.agent_name} falhou durante job {job['id']} "
            f"na etapa {step}."
        )

    async def transfer_job_via_robots(self, job, dest_machine_jid, beh):
        """Transfere um job para outra máquina usando robots (transport-cnp).

        Este método:

        1. Verifica se existem robots configurados.
        2. Obtém a posição da máquina de destino a partir do ambiente (`env`).
        3. Constrói um payload de transporte de job:
            - ``kind = "job"``
            - posição de origem e destino,
            - estado do job (pipeline, etapa atual, ticks restantes, etc.).
        4. Inicia um CNP de transporte com robots via protocolo
           ``"transport-cnp"``:
            - Envia CFP para todos os robots.
            - Recolhe PROPOSE/REFUSE.
            - Seleciona o robot com menor custo.
            - Envia REJECT para os restantes.
            - Envia ACCEPT-PROPOSAL ao robot vencedor.
        5. Aguarda um INFORM de conclusão de transporte. Se receber
           ``status="delivered"`` e ``kind="job"``, considera a transferência
           bem sucedida.

        Args:
            job (dict): Estado serializável do job a transferir
                (id, pipeline, current_step_idx, remaining_ticks, etc.).
            dest_machine_jid (str): JID da máquina de destino.
            beh (spade.behaviour.CyclicBehaviour): Comportamento chamador,
                usado para `send`/`receive` no contexto correto.

        Returns:
            bool: ``True`` se a transferência foi confirmada por INFORM,
            ``False`` caso contrário (incluindo timeouts ou ausência de robots).
        """
        if not self.robots:
            await self.log("[TRANSFER/ROBOT] ERRO: nenhum robot configurado.")
            return False

        dest_pos = None
        if self.env:
            for a in self.env.agents:
                if getattr(a, "is_machine", False) and str(a.jid) == dest_machine_jid:
                    dest_pos = list(getattr(a, "position", [0, 0]))
                    break

        if dest_pos is None:
            await self.log(
                f"[TRANSFER/ROBOT] ERRO: não encontrei posição da máquina {dest_machine_jid}."
            )
            return False

        origin_pos = list(self.position)

        task = {
            "kind": "job",
            "origin_pos": origin_pos,
            "dest_pos": dest_pos,
            "job": job,
            "destination_jid": dest_machine_jid,
        }

        thread_id = f"jobtrans-{job['id']}-{self.env.time if self.env else random.randint(0,9999)}"

        for r_jid in self.robots:
            m = Message(to=r_jid)
            m.set_metadata("protocol", "transport-cnp")
            m.set_metadata("performative", "cfp")
            m.set_metadata("thread", thread_id)
            m.body = json.dumps(task)
            await beh.send(m)

        await self.log(
            f"[TRANSFER/ROBOT] CFP enviado aos robots {self.robots} "
            f"para job {job['id']} (thread={thread_id})."
        )

        proposals = []
        deadline = asyncio.get_event_loop().time() + 3

        while asyncio.get_event_loop().time() < deadline:
            rep = await beh.receive(timeout=0.5)
            if not rep:
                continue
            if rep.metadata.get("protocol") != "transport-cnp":
                continue
            if rep.metadata.get("thread") != thread_id:
                continue

            pf = rep.metadata.get("performative")
            try:
                data = json.loads(rep.body) if rep.body else {}
            except Exception:
                data = {}

            if pf == "propose":
                cost = float(data.get("cost", 9999))
                proposals.append((str(rep.sender), cost))
            elif pf == "refuse":
                await self.log(
                    f"[TRANSFER/ROBOT] Robot {rep.sender} recusou "
                    f"(motivo={data.get('reason')})."
                )

        if not proposals:
            await self.log(
                f"[TRANSFER/ROBOT] Nenhum robot aceitou transportar job {job['id']}."
            )
            return False

        proposals.sort(key=lambda x: x[1])
        winner_jid, winner_cost = proposals[0]

        await self.log(
            f"[TRANSFER/ROBOT] Robot escolhido p/ job {job['id']}: "
            f"{winner_jid} (cost={winner_cost})."
        )

        for jid, _ in proposals[1:]:
            rej = Message(to=jid)
            rej.set_metadata("protocol", "transport-cnp")
            rej.set_metadata("performative", "reject-proposal")
            rej.set_metadata("thread", thread_id)
            rej.body = json.dumps({"reason": "not_selected"})
            await beh.send(rej)

        acc = Message(to=winner_jid)
        acc.set_metadata("protocol", "transport-cnp")
        acc.set_metadata("performative", "accept-proposal")
        acc.set_metadata("thread", thread_id)
        acc.body = json.dumps(task)
        await beh.send(acc)

        deadline_inf = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline_inf:
            rep = await beh.receive(timeout=0.5)
            if not rep:
                continue
            if rep.metadata.get("protocol") != "transport-cnp":
                continue
            if rep.metadata.get("thread") != thread_id:
                continue
            if rep.metadata.get("performative") != "inform":
                continue

            try:
                data = json.loads(rep.body) if rep.body else {}
            except Exception:
                data = {}

            if data.get("status") == "delivered" and data.get("kind") == "job":
                await self.log(
                    f"[TRANSFER/ROBOT] Transporte do job {job['id']} concluído "
                    f"por {winner_jid}. Job será entregue à máquina destino pelo robot."
                )
                if self.env:
                    self.env.metrics["jobs_transferred"] += 1
                return True

        await self.log(
            f"[TRANSFER/ROBOT] Timeout à espera de INFORM do robot p/ job {job['id']}."
        )
        return False

    class JobDispatchParticipant(CyclicBehaviour):
        """Participante no CNP de jobs com o Supervisor (job-dispatch).

        Este comportamento trata das mensagens do protocolo ``"job-dispatch"``:

        - CFP:
            * Se a máquina estiver falhada → REFUSE ``"failed"``.
            * Se a fila estiver cheia → REFUSE ``"queue_full"``.
            * Caso contrário, PROPOSE com custo = tamanho da fila.
        - ACCEPT-PROPOSAL:
            * Cria um novo job com o pipeline padrão e adiciona-o à fila.
            * Envia INFORM ao Supervisor a confirmar aceitação.
        - REJECT-PROPOSAL:
            * Apenas regista em log.
        """

        async def run(self):
            """Processa uma mensagem do CNP de dispatch por iteração.

            Em cada iteração:

            1. Tenta receber uma mensagem com timeout de 0.5 s.
            2. Se não for do protocolo ``"job-dispatch"``, ignora.
            3. Consoante o `performative`:
                - ``"cfp"`` → decide entre PROPOSE/REFUSE com base na fila
                  e no estado de falha.
                - ``"accept-proposal"`` → enfileira novo job.
                - ``"reject-proposal"`` → apenas log.
            """
            agent = self.agent

            msg = await self.receive(timeout=0.5)
            if not msg:
                return

            if msg.metadata.get("protocol") != "job-dispatch":
                return

            pf = msg.metadata.get("performative")
            thread_id = msg.metadata.get("thread")

            try:
                job_spec = json.loads(msg.body) if msg.body else {}
            except Exception:
                job_spec = {}

            if pf == "cfp":
                if agent.is_failed:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "job-dispatch")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "failed"})
                    await self.send(rep)
                    await agent.log(
                        f"[JOB-DISPATCH] REFUSE para {msg.sender} (falhada)."
                    )
                    return

                if len(agent.job_queue) >= agent.max_queue:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "job-dispatch")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "queue_full"})
                    await self.send(rep)

                    await agent.log(
                        f"[JOB-DISPATCH] REFUSE para {msg.sender} "
                        f"(fila cheia: {len(agent.job_queue)})"
                    )
                    return

                cost = len(agent.job_queue)

                rep = Message(to=str(msg.sender))
                rep.set_metadata("protocol", "job-dispatch")
                rep.set_metadata("performative", "propose")
                rep.set_metadata("thread", thread_id)
                rep.body = json.dumps({"status": "available", "cost": cost})
                await self.send(rep)

                await agent.log(
                    f"[JOB-DISPATCH] PROPOSE para {msg.sender} com cost={cost}"
                )
                return

            if pf == "accept-proposal":
                job_id = job_spec.get("job_id")

                job = {
                    "id": job_id,
                    "pipeline": agent.pipeline_template.copy(),
                    "current_step_idx": 0,
                    "materials_ok": False,
                }
                agent.job_queue.append(job)

                await agent.log(
                    f"[JOB-DISPATCH] ACCEPT de {msg.sender}. "
                    f"Job {job_id} na fila (fila={len(agent.job_queue)})."
                )

                rep = Message(to=str(msg.sender))
                rep.set_metadata("protocol", "job-dispatch")
                rep.set_metadata("performative", "inform")
                rep.set_metadata("thread", thread_id)
                rep.body = json.dumps({"status": "accepted", "job_id": job_id})
                await self.send(rep)
                return

            if pf == "reject-proposal":
                await agent.log(
                    f"[JOB-DISPATCH] REJECT recebido de {msg.sender}."
                )
                return

    async def request_materials_via_cnp(self, beh, job):
        """Lança um CNP ``"supply-cnp"`` aos suppliers para obter materiais.

        Este método:

        1. Verifica se existem suppliers configurados.
        2. Envia uma CFP para todos os suppliers com:
            - materiais necessários (`material_requirements`);
            - posição da máquina.
        3. Recolhe PROPOSE/REFUSE dos suppliers:
            - Seleciona o supplier com menor custo.
            - Envia REJECT aos restantes.
            - Envia ACCEPT-PROPOSAL ao vencedor.
        4. Aguarda INFORM final:
            - Se receber ``status="delivered"``:
                * marca `job["materials_ok"] = True`;
                * atualiza métricas de sucesso.
            - Em caso de timeout:
                * atualiza métricas de falha.

        Args:
            beh (spade.behaviour.CyclicBehaviour): Comportamento chamador,
                usado para `send`/`receive` durante o CNP.
            job (dict): Dicionário de estado do job para o qual os materiais
                são pedidos.

        Returns:
            bool: ``True`` se os materiais foram entregues com sucesso,
            ``False`` caso contrário.
        """
        if not self.suppliers:
            await self.log("[SUPPLY] Nenhum supplier configurado. Job não avança.")
            return False

        materials_needed = self.material_requirements
        machine_pos = list(self.position)

        payload = {
            "materials": materials_needed,
            "machine_pos": machine_pos,
        }

        thread_id = f"supply-{self.agent_name}-{random.randint(0, 9999)}"

        for sup_jid in self.suppliers:
            m = Message(to=sup_jid)
            m.set_metadata("protocol", "supply-cnp")
            m.set_metadata("performative", "cfp")
            m.set_metadata("thread", thread_id)
            m.body = json.dumps(payload)
            await beh.send(m)

        await self.log(
            f"[SUPPLY] CFP enviado aos suppliers {self.suppliers} "
            f"para materiais={materials_needed} (thread={thread_id})"
        )

        proposals = []
        deadline = asyncio.get_event_loop().time() + 3

        while asyncio.get_event_loop().time() < deadline:
            rep = await beh.receive(timeout=0.5)
            if not rep:
                continue
            if rep.metadata.get("protocol") != "supply-cnp":
                continue
            if rep.metadata.get("thread") != thread_id:
                continue

            pf = rep.metadata.get("performative")
            if pf == "propose":
                try:
                    data = json.loads(rep.body)
                    cost = float(data.get("cost", 9999))
                    proposals.append((str(rep.sender), cost, data))
                except Exception:
                    continue
            elif pf == "refuse":
                await self.log(f"[SUPPLY] Supplier {rep.sender} recusou (sem stock).")

        if not proposals:
            await self.log("[SUPPLY] Nenhuma proposta recebida. Falha de supply.")
            if self.env:
                self.env.metrics["supply_requests_failed"] += 1
            return False

        proposals.sort(key=lambda x: x[1])
        winner_jid, winner_cost, winner_data = proposals[0]

        await self.log(
            f"[SUPPLY] Supplier vencedor={winner_jid} com cost={winner_cost:.2f}. "
            "Enviando ACCEPT-PROPOSAL."
        )

        for sup_jid, _, _ in proposals[1:]:
            rej = Message(to=sup_jid)
            rej.set_metadata("protocol", "supply-cnp")
            rej.set_metadata("performative", "reject-proposal")
            rej.set_metadata("thread", thread_id)
            rej.body = json.dumps({"reason": "not_selected"})
            await beh.send(rej)

        acc = Message(to=winner_jid)
        acc.set_metadata("protocol", "supply-cnp")
        acc.set_metadata("performative", "accept-proposal")
        acc.set_metadata("thread", thread_id)
        acc.body = json.dumps({
            "materials": materials_needed,
            "machine_pos": machine_pos,
        })
        await beh.send(acc)

        deadline_inf = asyncio.get_event_loop().time() + 10
        while asyncio.get_event_loop().time() < deadline_inf:
            rep = await beh.receive(timeout=0.5)
            if not rep:
                continue
            if rep.metadata.get("protocol") != "supply-cnp":
                continue
            if rep.metadata.get("thread") != thread_id:
                continue
            if rep.metadata.get("performative") != "inform":
                continue

            try:
                data = json.loads(rep.body)
            except Exception:
                data = {}

            status = data.get("status")
            if status == "delivered":
                await self.log(
                    f"[SUPPLY] Materiais entregues pelo supplier {winner_jid}: "
                    f"{data.get('materials')}"
                )
                if self.env:
                    self.env.metrics["supply_requests_ok"] += 1
                job["materials_ok"] = True
                return True

        await self.log("[SUPPLY] Timeout à espera de INFORM do supplier.")
        if self.env:
            self.env.metrics["supply_requests_failed"] += 1
        return False

    class JobExecutor(CyclicBehaviour):
        """Executa o pipeline de jobs na máquina e trata de falhas.

        Responsabilidades:

        - Selecionar jobs da fila quando não há `current_job`.
        - Iniciar ou retomar a execução do job na etapa correta.
        - Em ``step0``:
            * Se não tiver ``materials_ok``, pedir materiais via CNP aos suppliers.
            * Em caso de falha de supply, marcar job como perdido.
        - Em ``step1``, ``step2``, ``step3``:
            * A cada tick, decrementar `current_job_ticks`.
            * Com probabilidade `failure_rate`, provocar falha da máquina.
            * Quando os ticks da etapa acabam, avançar para a etapa seguinte
              ou concluir o job (atualizando métricas).
        """

        async def run(self):
            """Executa um ciclo de processamento do job.

            Em cada iteração:

            1. Se a máquina estiver falhada:
                - Incrementa a métrica de `downtime_ticks`.
                - Aguard
            2. Se não existir `current_job` mas houver jobs na fila:
                - Extrai o próximo job.
                - Garante que tem `pipeline`, `current_step_idx` e `materials_ok`.
                - Define `current_job_ticks` (podendo vir de `remaining_ticks`
                  em caso de transferência).
            3. Se o `current_step` for ``"step0"``:
                - Se não tiver materiais, chama
                  :meth:`MachineAgent.request_materials_via_cnp`.
                - Em caso de sucesso, avança imediatamente para ``step1``
                  ou termina o job se só houver step0.
            4. Para steps posteriores:
                - Pode ocorrer falha com probabilidade `failure_rate`.
                - Decrementa `current_job_ticks` e regista progresso.
                - Ao terminar a etapa, avança no pipeline ou marca job concluído.
            """
            agent = self.agent

            if agent.is_failed:
                if agent.env:
                    agent.env.metrics["downtime_ticks"] += 1
                await asyncio.sleep(1)
                return

            if agent.current_job is None and agent.job_queue:
                agent.current_job = agent.job_queue.pop(0)
                job = agent.current_job

                job.setdefault("pipeline", agent.pipeline_template.copy())
                if "current_step_idx" not in job:
                    job["current_step_idx"] = 0
                if "materials_ok" not in job:
                    job["materials_ok"] = job["current_step_idx"] > 0

                pipeline = job["pipeline"]
                idx = job["current_step_idx"]
                current_step = pipeline[idx]

                if "remaining_ticks" in job:
                    agent.current_job_ticks = job.pop("remaining_ticks")
                else:
                    agent.current_job_ticks = agent.step_durations[current_step]

                await agent.log(
                    f"[JOB] Início/retoma do job {job['id']} na etapa {current_step} "
                    f"(ticks_step={agent.current_job_ticks})"
                )

            job = agent.current_job
            if job is None:
                await asyncio.sleep(0.5)
                return

            pipeline = job["pipeline"]
            idx = job["current_step_idx"]
            current_step = pipeline[idx]

            if current_step == "step0":
                if not job.get("materials_ok", False):
                    ok = await agent.request_materials_via_cnp(self, job)
                    if not ok:
                        await agent.log(
                            f"[JOB] Job {job['id']} cancelado por falta de materiais."
                        )
                        if agent.env:
                            agent.env.metrics["jobs_lost"] += 1
                        agent.current_job = None
                        agent.current_job_ticks = 0
                        await asyncio.sleep(0.5)
                        return

                    await agent.log(
                        f"[JOB] Job {job['id']} concluiu step0 (materiais consumidos)."
                    )

                    if idx < len(pipeline) - 1:
                        job["current_step_idx"] += 1
                        next_step = pipeline[job["current_step_idx"]]
                        agent.current_job_ticks = agent.step_durations[next_step]
                        await agent.log(
                            f"[JOB] Job {job['id']} transita para {next_step} "
                            f"(ticks_step={agent.current_job_ticks})"
                        )
                        await asyncio.sleep(0.5)
                        return
                    else:
                        await agent.log(
                            f"[JOB] Job {job['id']} concluído (após step0)."
                        )
                        if agent.env:
                            agent.env.metrics["jobs_completed"] += 1
                        agent.current_job = None
                        agent.current_job_ticks = 0
                        await asyncio.sleep(0.5)
                        return

            pipeline = job["pipeline"]
            idx = job["current_step_idx"]
            current_step = pipeline[idx]

            if current_step != "step0":
                if random.random() < agent.failure_rate:
                    await agent.handle_failure(job)
                    return

            agent.current_job_ticks -= 1
            await agent.log(
                f"[JOB] job_id={job['id']} etapa={current_step}, "
                f"ticks_rest={agent.current_job_ticks}"
            )

            if agent.current_job_ticks <= 0:
                if idx < len(pipeline) - 1:
                    job["current_step_idx"] += 1
                    next_step = pipeline[job["current_step_idx"]]
                    agent.current_job_ticks = agent.step_durations[next_step]

                    await agent.log(
                        f"[JOB] Job {job['id']} entrou na etapa {next_step} "
                        f"(ticks_step={agent.current_job_ticks})"
                    )
                else:
                    await agent.log(
                        f"[JOB] Job {job['id']} concluído (pipeline completo)."
                    )
                    if agent.env:
                        agent.env.metrics["jobs_completed"] += 1
                    agent.current_job = None
                    agent.current_job_ticks = 0

            await asyncio.sleep(1)


    class MaintenanceRequester(CyclicBehaviour):
        """Inicia CNP de manutenção com as equipas de manutenção.

        Este comportamento:

        - Só atua quando a máquina está falhada (`is_failed`) e ainda não
          foi feito pedido de manutenção (`maintenance_requested` = False).
        - Envia CFP para todas as `maintenance_crews` com:
            * JID da máquina;
            * posição da máquina.
        - Recolhe PROPOSE/REFUSE das crews.
        - Escolhe a crew com menor custo e:
            * Envia REJECT às outras;
            * Envia ACCEPT-PROPOSAL à vencedora com tempo de reparação.
        - Atualiza flags (`repair_in_progress`) e métricas
          (``repairs_started``) no ambiente.
        """

        async def run(self):
            """Executa um ciclo de pedido de manutenção.

            Lógica:

            1. Se a máquina não estiver falhada ou já tiver pedido manutenção,
               dorme e sai.
            2. Caso contrário:
                - Verifica se existem crews configuradas.
                - Envia CFP a todas as crews via protocolo ``"maintenance-cnp"``.
                - Recolhe respostas durante um tempo limite.
                - Se não houver propostas:
                    * Regista no log que não há crews disponíveis.
                - Se houver:
                    * Escolhe a proposta de menor custo.
                    * Envia REJECT às restantes.
                    * Envia ACCEPT-PROPOSAL à vencedora com dados da máquina
                      e `repair_time`.
                    * Marca que há reparação em progresso.
            """
            agent = self.agent

            if not agent.is_failed or agent.maintenance_requested:
                await asyncio.sleep(0.5)
                return

            agent.maintenance_requested = True

            if not agent.maintenance_crews:
                await agent.log("[MAINT-REQ] Nenhuma maintenance crew configurada.")
                return

            thread_id = f"maint-{agent.agent_name}-{agent.env.time}"
            payload = {
                "machine_jid": str(agent.jid),
                "machine_pos": list(agent.position),
            }

            for crew_jid in agent.maintenance_crews:
                m = Message(to=crew_jid)
                m.set_metadata("protocol", "maintenance-cnp")
                m.set_metadata("performative", "cfp")
                m.set_metadata("thread", thread_id)
                m.body = json.dumps(payload)
                await self.send(m)

            await agent.log(
                f"[MAINT-REQ] CFP enviado às crews {agent.maintenance_crews} "
                f"(thread={thread_id})"
            )

            proposals = []
            deadline = asyncio.get_event_loop().time() + 3

            while asyncio.get_event_loop().time() < deadline:
                rep = await self.receive(timeout=0.5)
                if not rep:
                    continue
                if rep.metadata.get("protocol") != "maintenance-cnp":
                    continue
                if rep.metadata.get("thread") != thread_id:
                    continue

                pf = rep.metadata.get("performative")
                try:
                    data = json.loads(rep.body) if rep.body else {}
                except Exception:
                    data = {}

                if pf == "propose":
                    cost = float(data.get("cost", 9999))
                    repair_time = int(data.get("repair_time", 5))
                    proposals.append((str(rep.sender), cost, repair_time))
                elif pf == "refuse":
                    await agent.log(
                        f"[MAINT-REQ] REFUSE de {rep.sender} "
                        f"(motivo={data.get('reason')})"
                    )

            if not proposals:
                await agent.log("[MAINT-REQ] Nenhuma crew disponível. Tentará depois.")
                return

            proposals.sort(key=lambda x: x[1])
            winner_jid, winner_cost, winner_rt = proposals[0]

            await agent.log(
                f"[MAINT-REQ] Crew escolhida: {winner_jid} (cost={winner_cost:.2f}, "
                f"repair_time={winner_rt})."
            )

            for crew_jid, _, _ in proposals[1:]:
                rej = Message(to=crew_jid)
                rej.set_metadata("protocol", "maintenance-cnp")
                rej.set_metadata("performative", "reject-proposal")
                rej.set_metadata("thread", thread_id)
                rej.body = json.dumps({"reason": "not_selected"})
                await self.send(rej)

            acc = Message(to=winner_jid)
            acc.set_metadata("protocol", "maintenance-cnp")
            acc.set_metadata("performative", "accept-proposal")
            acc.set_metadata("thread", thread_id)
            acc.body = json.dumps({
                "machine_jid": str(agent.jid),
                "machine_pos": list(agent.position),
                "repair_time": winner_rt,
            })
            await self.send(acc)

            agent.repair_in_progress = True
            if agent.env:
                agent.env.metrics["repairs_started"] += 1

    class MaintenanceResponseHandler(CyclicBehaviour):
        """Trata respostas INFORM das equipas de manutenção.

        Este comportamento escuta o protocolo ``"maintenance-cnp"`` e:

        - Quando recebe INFORM com ``status="repair_started"``:
            * Regista no log que a reparação começou.
        - Quando recebe INFORM com ``status="repair_completed"``:
            * Marca a máquina como operacional (`is_failed = False`).
            * Limpa flags de reparação (`repair_in_progress`,
              `maintenance_requested`).
            * Regista no log a conclusão da reparação.
        """

        async def run(self):
            """Processa uma mensagem de resposta de manutenção, se existir."""
            agent = self.agent

            msg = await self.receive(timeout=0.5)
            if not msg:
                return

            if msg.metadata.get("protocol") != "maintenance-cnp":
                return

            pf = msg.metadata.get("performative")

            try:
                data = json.loads(msg.body) if msg.body else {}
            except Exception:
                data = {}

            status = data.get("status")

            if pf == "inform":
                if status == "repair_started":
                    await agent.log("[MAINT] Reparação iniciada.")
                elif status == "repair_completed":
                    agent.is_failed = False
                    agent.repair_in_progress = False
                    agent.maintenance_requested = False

                    await agent.log("[MAINT] Reparação concluída. Máquina operacional.")


    class JobTransferInitiator(CyclicBehaviour):
        """Inicia CNP de transferência de job para outras máquinas.

        Este comportamento é usado quando:

        - A máquina está falhada (`is_failed`).
        - Existe um job atual (`current_job`).
        - Ainda não foi feita/terminada uma tentativa de transferência
          (`job_transfer_done` = False).

        Passos:

        1. Descobre outras máquinas registadas no ambiente (`env.agents`).
        2. Empacota o estado do job (pipeline, índice, materials_ok,
           remaining_ticks, etc.).
        3. Envia CFP a essas máquinas usando protocolo ``"transfer-cnp"``.
        4. Recolhe PROPOSE/REFUSE e escolhe a máquina com menor custo
           (por ex., fila mais pequena).
        5. Envia REJECT aos restantes, ACCEPT-PROPOSAL ao vencedor.
        6. Chama :meth:`MachineAgent.transfer_job_via_robots` para transportar
           o job até à máquina destino via robots.
        7. Se a transferência for bem sucedida:
            * Liberta o job atual;
            * Atualiza `job_transfer_done`.
        """

        async def run(self):
            """Executa um ciclo de tentativa de transferência de job."""
            agent = self.agent

            if (
                not agent.is_failed
                or agent.current_job is None
                or agent.job_transfer_done
            ):
                await asyncio.sleep(0.5)
                return

            if not agent.env:
                await asyncio.sleep(0.5)
                return

            candidates = [
                a for a in agent.env.agents
                if getattr(a, "is_machine", False) and a.jid != agent.jid
            ]

            if not candidates:
                await agent.log("[TRANSFER] Nenhuma outra máquina disponível.")
                agent.job_transfer_done = True
                await asyncio.sleep(0.5)
                return

            job = agent.current_job

            job_data = {
                "id": job["id"],
                "pipeline": job["pipeline"],
                "current_step_idx": job["current_step_idx"],
                "materials_ok": job.get("materials_ok", False),
                "remaining_ticks": agent.current_job_ticks,
            }

            payload = {
                "job": job_data,
                "origin_jid": str(agent.jid),
            }

            thread_id = f"transfer-{job['id']}-{agent.env.time}"

            for m in candidates:
                msg = Message(to=str(m.jid))
                msg.set_metadata("protocol", "transfer-cnp")
                msg.set_metadata("performative", "cfp")
                msg.set_metadata("thread", thread_id)
                msg.body = json.dumps(payload)
                await self.send(msg)

            await agent.log(
                f"[TRANSFER] CFP enviado a máquinas "
                f"{[str(m.jid) for m in candidates]} para job {job['id']} "
                f"(thread={thread_id})."
            )

            proposals = []
            deadline = asyncio.get_event_loop().time() + 3

            while asyncio.get_event_loop().time() < deadline:
                rep = await self.receive(timeout=0.5)
                if not rep:
                    continue
                if rep.metadata.get("protocol") != "transfer-cnp":
                    continue
                if rep.metadata.get("thread") != thread_id:
                    continue

                pf = rep.metadata.get("performative")
                try:
                    data = json.loads(rep.body) if rep.body else {}
                except Exception:
                    data = {}

                if pf == "propose":
                    cost = int(data.get("cost", 999))
                    proposals.append((str(rep.sender), cost))
                elif pf == "refuse":
                    await agent.log(
                        f"[TRANSFER] REFUSE de {rep.sender} "
                        f"(motivo={data.get('reason')})."
                    )

            if not proposals:
                await agent.log("[TRANSFER] Nenhuma máquina aceitou receber o job.")
                agent.job_transfer_done = True
                await asyncio.sleep(0.5)
                return

            proposals.sort(key=lambda x: x[1])
            winner_jid, winner_cost = proposals[0]

            await agent.log(
                f"[TRANSFER] Máquina escolhida para receber job {job['id']}: "
                f"{winner_jid} (cost={winner_cost}, candidatos={proposals})"
            )

            for jid, _ in proposals[1:]:
                rej = Message(to=jid)
                rej.set_metadata("protocol", "transfer-cnp")
                rej.set_metadata("performative", "reject-proposal")
                rej.set_metadata("thread", thread_id)
                rej.body = json.dumps({"reason": "not_selected"})
                await self.send(rej)

            acc = Message(to=winner_jid)
            acc.set_metadata("protocol", "transfer-cnp")
            acc.set_metadata("performative", "accept-proposal")
            acc.set_metadata("thread", thread_id)
            acc.body = json.dumps({"job_id": job["id"]})
            await self.send(acc)

            ok = await agent.transfer_job_via_robots(job_data, winner_jid, self)

            if ok:
                agent.current_job = None
                agent.current_job_ticks = 0
                agent.job_transfer_done = True
                await agent.log(
                    f"[TRANSFER] Job {job['id']} transferido com sucesso para {winner_jid} via robots."
                )
            else:
                await agent.log(
                    f"[TRANSFER] Falha na transferência via robots para {winner_jid}."
                )
                agent.job_transfer_done = True

    class JobTransferParticipant(CyclicBehaviour):
        """Participa no CNP de transferência de jobs como máquina candidata.

        Lado da máquina que pode receber jobs transferidos:

        - CFP:
            * Se máquina estiver falhada ou fila cheia, REFUSE.
            * Caso contrário, PROPOSE com custo = tamanho da fila.
        - ACCEPT-PROPOSAL:
            * Apenas regista que foi escolhida, aguardando entrega via robot.
        - REJECT-PROPOSAL:
            * Apenas regista que não foi escolhida.
        """

        async def run(self):
            """Processa mensagens do protocolo ``"transfer-cnp"`` por iteração."""
            agent = self.agent

            msg = await self.receive(timeout=0.5)
            if not msg:
                return

            if msg.metadata.get("protocol") != "transfer-cnp":
                return

            pf = msg.metadata.get("performative")
            thread_id = msg.metadata.get("thread")

            try:
                data = json.loads(msg.body) if msg.body else {}
            except Exception:
                data = {}

            if pf == "cfp":
                if agent.is_failed:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "transfer-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "failed"})
                    await self.send(rep)
                    return

                if len(agent.job_queue) >= agent.max_queue:
                    rep = Message(to=str(msg.sender))
                    rep.set_metadata("protocol", "transfer-cnp")
                    rep.set_metadata("performative", "refuse")
                    rep.set_metadata("thread", thread_id)
                    rep.body = json.dumps({"reason": "queue_full"})
                    await self.send(rep)
                    return

                cost = len(agent.job_queue)

                rep = Message(to=str(msg.sender))
                rep.set_metadata("protocol", "transfer-cnp")
                rep.set_metadata("performative", "propose")
                rep.set_metadata("thread", thread_id)
                rep.body = json.dumps({"cost": cost})
                await self.send(rep)

                await agent.log(
                    f"[TRANSFER] PROPOSE enviado para {msg.sender} (cost={cost})."
                )
                return

            if pf == "accept-proposal":
                job_id = data.get("job_id")
                await agent.log(
                    f"[TRANSFER] ACCEPT-PROPOSAL recebido para job {job_id}. "
                    "Aguardando entrega via robot..."
                )
                return

            if pf == "reject-proposal":
                await agent.log("[TRANSFER] REJECT recebido (não fui escolhido).")
                return

    class JobTransferReceiver(CyclicBehaviour):
        """Recebe jobs entregues por robots via protocolo ``"job-transfer"``.

        Este comportamento:

        - Escuta mensagens com protocolo ``"job-transfer"``.
        - Se `status == "job_delivered"`:
            * Valida o dicionário de job.
            * Adiciona o job à `job_queue`.
            * Regista no log o tamanho atualizado da fila.
        """

        async def run(self):
            """Processa uma mensagem de entrega de job."""
            agent = self.agent
            msg = await self.receive(timeout=0.5)
            if not msg:
                return

            if msg.metadata.get("protocol") != "job-transfer":
                return

            try:
                data = json.loads(msg.body) if msg.body else {}
            except Exception:
                data = {}

            if data.get("status") != "job_delivered":
                return

            job = data.get("job")
            if not isinstance(job, dict):
                await agent.log("[JOB-TRANSFER] ERRO: job inválido recebido.")
                return

            agent.job_queue.append(job)
            await agent.log(
                f"[JOB-TRANSFER] Job {job.get('id')} entregue por robot e "
                f"adicionado à fila (tam={len(agent.job_queue)})."
            )
