import asyncio
import json
import random

from agents.base_agent import FactoryAgent
from spade.behaviour import CyclicBehaviour
from spade.message import Message


class SupervisorAgent(FactoryAgent):
    """Agente supervisor responsável pelo agendamento e atribuição de jobs.

    Este agente é responsável por criar jobs periodicamente e coordenar um
    leilão/negociação com as máquinas disponíveis usando um protocolo Contract Net Protocol (CNP). Cada job é enviado como CFP
    para todas as máquinas registadas, que podem propor um custo ou recusar
    o job. O supervisor escolhe a proposta com menor custo (desempate aleatório em caso de empate). 
    Envia ``accept-proposal`` para a máquina vencedora e ``reject-proposal``
    para as restantes.
    
    """

    def __init__(
        self,
        jid,
        password,
        env=None,
        machines=None,
        job_dispatch_every=5,
    ):
        """Inicializa o agente supervisor.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Password associada ao JID.
            env: Referência ao ambiente/simulador partilhado, usado para obter
                o tempo atual (ticks) e registar métricas globais.
            machines (list[str] | None): Lista de JIDs das máquinas que irão
                receber CFPs. Se ``None``, é usada uma lista vazia.
            job_dispatch_every (int): Número de ticks entre despachos sucessivos
                de novos jobs.
        """
        super().__init__(jid, password, env=env)
        self.machines = machines or []
        self.job_dispatch_every = job_dispatch_every
        self.last_dispatch_time = 0
        self.job_counter = 0

    async def setup(self):
        """Configura o agente supervisor após o arranque.

        Este método:

        1. Regista o agente no ambiente, (via :class:`FactoryAgent`).
        2. Escreve uma mensagem de log com a configuração atual.
        3. Adiciona o comportamento :class:`JobDispatcher` responsável por
           criar e despachar jobs ciclicamente.
        """
        await super().setup()
        await self.log(
            f"Supervisor pronto. job_dispatch_every={self.job_dispatch_every}, "
            f"máquinas={self.machines}"
        )
        self.add_behaviour(self.JobDispatcher())

    class JobDispatcher(CyclicBehaviour):
        """Comportamento cíclico responsável por criar e negociar jobs.

        Em cada execução do ciclo, este comportamento:

        1. Verifica o tempo atual do ambiente.
        2. Se tiver passado pelo menos ``job_dispatch_every`` ticks desde o
           último job, cria um novo job com uma duração aleatória.
        3. Envia uma CFP (*Call For Proposals*) para todas as máquinas registadas.
        4. Aguarda respostas (``propose`` ou ``refuse``) durante uma janela
           de tempo limitada.
        5. Seleciona a proposta de menor custo e envia:
           - ``accept-proposal`` à máquina vencedora;
           - ``reject-proposal`` às restantes máquinas que propuseram.
        """

        async def run(self):
            """Executa um ciclo de despacho e negociação de jobs.

            Este método é chamado repetidamente pelo motor de comportamentos
            do SPADE. Em cada iteração pode ou não ser criado um novo job,
            dependendo do tempo decorrido desde o último despacho.

            Protocolo de mensagens:
                - CFP:
                    * metadata:
                        - ``protocol`` = ``"job-dispatch"``
                        - ``performative`` = ``"cfp"``
                        - ``thread`` = ID único para o job
                    * body (JSON):
                        - ``job_id`` (int)
                        - ``duration`` (int)
                - PROPOSE:
                    * metadata:
                        - ``protocol`` = ``"job-dispatch"``
                        - ``performative`` = ``"propose"``
                        - ``thread`` = mesmo ID do job
                    * body (JSON):
                        - ``cost`` (numérico): custo reportado pela máquina.
                - REFUSE:
                    * metadata:
                        - ``protocol`` = ``"job-dispatch"``
                        - ``performative`` = ``"refuse"``
                        - ``thread`` = mesmo ID do job
                    * body (JSON, opcional):
                        - ``reason`` (str): motivo da recusa.
                - ACCEPT-PROPOSAL / REJECT-PROPOSAL:
                    * metadata:
                        - ``protocol`` = ``"job-dispatch"``
                        - ``performative`` = ``"accept-proposal"`` ou
                          ``"reject-proposal"``
                        - ``thread`` = mesmo ID do job
                    * body:
                        - No caso de ``accept-proposal``: especificação do job.
                        - No caso de ``reject-proposal``: motivo da rejeição.

            Side Effects:
                - Atualiza ``env.metrics["jobs_created"]``.
                - Escreve mensagens de log com o estado do leilão e decisões
                  de seleção de máquinas.

            """
            agent = self.agent
            env = agent.env
            t = env.time

            if t > 0 and (t - agent.last_dispatch_time) >= agent.job_dispatch_every:
                agent.last_dispatch_time = t
                agent.job_counter += 1

                job_id = agent.job_counter
                job_spec = {
                    "job_id": job_id,
                    "duration": random.randint(3, 8),
                }

                thread_id = f"job-dispatch-{job_id}-{t}"

                for m_jid in agent.machines:
                    msg = Message(to=m_jid)
                    msg.set_metadata("protocol", "job-dispatch")
                    msg.set_metadata("performative", "cfp")
                    msg.set_metadata("thread", thread_id)
                    msg.body = json.dumps(job_spec)
                    await self.send(msg)

                await agent.log(
                    f"[SCHEDULER] CFP enviado para máquinas {agent.machines} "
                    f"com job_id={job_id}, duration={job_spec['duration']}"
                )

                if env:
                    env.metrics["jobs_created"] += 1

                proposals = []
                deadline = asyncio.get_event_loop().time() + 3

                while asyncio.get_event_loop().time() < deadline:
                    rep = await self.receive(timeout=0.5)
                    if not rep:
                        continue
                    if rep.metadata.get("protocol") != "job-dispatch":
                        continue
                    if rep.metadata.get("thread") != thread_id:
                        continue

                    pf = rep.metadata.get("performative")
                    try:
                        data = json.loads(rep.body) if rep.body else {}
                    except Exception:
                        data = {}

                    if pf == "propose":
                        cost = data.get("cost")
                        if cost is not None:
                            proposals.append((str(rep.sender), cost))
                    elif pf == "refuse":
                        await agent.log(
                            f"[SCHEDULER] REFUSE de {rep.sender} "
                            f"(motivo={data.get('reason')})"
                        )

                if not proposals:
                    await agent.log(
                        f"[SCHEDULER] Nenhuma máquina aceitou job {job_id}."
                    )
                    await asyncio.sleep(0.5)
                    return

                min_cost = min(cost for _, cost in proposals)
                best_candidates = [
                    (jid, cost) for jid, cost in proposals if cost == min_cost
                ]
                winner_jid, winner_cost = random.choice(best_candidates)

                await agent.log(
                    f"[SCHEDULER] Máquina escolhida para job {job_id}: "
                    f"{winner_jid} (cost={winner_cost}, candidatos={proposals})"
                )

                for jid, _ in proposals:
                    if jid == winner_jid:
                        acc = Message(to=jid)
                        acc.set_metadata("protocol", "job-dispatch")
                        acc.set_metadata("performative", "accept-proposal")
                        acc.set_metadata("thread", thread_id)
                        acc.body = json.dumps(job_spec)
                        await self.send(acc)
                    else:
                        rej = Message(to=jid)
                        rej.set_metadata("protocol", "job-dispatch")
                        rej.set_metadata("performative", "reject-proposal")
                        rej.set_metadata("thread", thread_id)
                        rej.body = json.dumps({"reason": "not_selected"})
                        await self.send(rej)

            await asyncio.sleep(0.5)