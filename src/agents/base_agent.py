from spade.agent import Agent
import datetime


class FactoryAgent(Agent):
    """Agente base para todos os agentes da fábrica.

    Esta classe fornece funcionalidades comuns a todos os agentes do
    sistema, nomeadamente:

    Referência ao ambiente de simulação (`env`).
    Registo automático no ambiente durante o `setup`.
    Método de logging padronizado.
    A ideia é que todos os outros agentes (máquinas, supervisor, robots,
    manutenção, etc.) herdem desta classe para manter um comportamento
    comum.
    
    """

    def __init__(self, jid, password, env=None):
        """Inicializa um agente de fábrica genérico.

        Args:
            jid (str): JID XMPP do agente.
            password (str): Password associada ao JID.
            env: Ambiente/simulador onde o agente será registado. Se for
                ``None``, nenhuma integração automática com ambiente é feita.
        """
        super().__init__(jid, password)
        self.env = env

    async def setup(self):
        """Executa a configuração inicial do agente após o arranque.

        Este método é chamado automaticamente pelo SPADE quando o agente
        arranca. A implementação:

        1. Regista o agente no ambiente, se `env` não for ``None``.
        2. Escreve uma mensagem de log indicando que o agente foi iniciado.

        """
        if self.env:
            self.env.register_agent(self)
        await self.log("iniciado.")

    async def log(self, msg: str):
        """Escreve uma mensagem de log com timestamp e JID do agente.

        A mensagem é impressa no stdout no formato:

        ``[HH:MM:SS][jid] mensagem``

        Args:
            msg (str): Texto a ser registado no log.
        """
        now = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{now}][{self.jid}] {msg}")
