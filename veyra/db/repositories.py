from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from veyra.db.models import Account, AttackLog, DamageTracker, SessionStats, TaskConfig
from veyra.security import decrypt, encrypt


class AccountRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, name: str, email: str, password: str) -> Account:
        account = Account(name=name, email=email, password_enc=encrypt(password))
        self.session.add(account)
        await self.session.flush()
        return account

    async def get(self, account_id: int) -> Account | None:
        return await self.session.get(Account, account_id)

    async def get_by_email(self, email: str) -> Account | None:
        result = await self.session.execute(select(Account).where(Account.email == email))
        return result.scalar_one_or_none()

    async def list_active(self) -> list[Account]:
        result = await self.session.execute(
            select(Account).where(Account.is_active.is_(True))
        )
        return list(result.scalars().all())

    async def list_all(self) -> list[Account]:
        result = await self.session.execute(select(Account))
        return list(result.scalars().all())

    async def update_session(self, account_id: int, cookies_json: str, game_user_id: str) -> None:
        account = await self.get(account_id)
        if account:
            account.session_cookies = cookies_json
            account.game_user_id = game_user_id

    async def delete(self, account_id: int) -> None:
        account = await self.get(account_id)
        if account:
            await self.session.delete(account)

    def get_password(self, account: Account) -> str:
        return decrypt(account.password_enc)


class DamageTrackerRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, account_id: int, monster_id: str) -> int:
        tracker = await self.session.get(DamageTracker, (account_id, monster_id))
        return tracker.damage_dealt if tracker else 0

    async def upsert(self, account_id: int, monster_id: str, damage: int) -> None:
        tracker = await self.session.get(DamageTracker, (account_id, monster_id))
        if tracker:
            tracker.damage_dealt = damage
        else:
            self.session.add(
                DamageTracker(account_id=account_id, monster_id=monster_id, damage_dealt=damage)
            )

    async def get_all_for_account(self, account_id: int) -> dict[str, int]:
        result = await self.session.execute(
            select(DamageTracker).where(DamageTracker.account_id == account_id)
        )
        return {t.monster_id: t.damage_dealt for t in result.scalars().all()}


class AttackLogRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(
        self,
        account_id: int,
        monster_id: str,
        monster_name: str,
        wave: int,
        damage_dealt: int,
        stamina_spent: int,
        result: str,
    ) -> AttackLog:
        log = AttackLog(
            account_id=account_id,
            monster_id=monster_id,
            monster_name=monster_name,
            wave=wave,
            damage_dealt=damage_dealt,
            stamina_spent=stamina_spent,
            result=result,
        )
        self.session.add(log)
        await self.session.flush()
        return log


class SessionStatsRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, account_id: int, task_type: str) -> SessionStats:
        stats = SessionStats(account_id=account_id, task_type=task_type)
        self.session.add(stats)
        await self.session.flush()
        return stats

    async def update(self, stats: SessionStats) -> None:
        # Object is already tracked by session; just flush
        await self.session.flush()
