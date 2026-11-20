from aiogram.fsm.state import State, StatesGroup


class SetupStates(StatesGroup):
    choosing_currency = State()
    choosing_bank = State()
    entering_custom_bank_name = State()
    entering_api_url = State()
    entering_json_path = State()
    entering_api_key = State()
    entering_threshold = State()
    entering_step = State()
    choosing_interval = State()