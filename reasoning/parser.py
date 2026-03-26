from typing import TypeVar, Type, Any

T = TypeVar('T')

def parse_model_response(response: Any, model_type: Type[T]) -> T:
    """
    Parses and validates the model's response against a Pydantic model.
    
    This provides runtime type checking, unlike `cast`, which is only a hint
    for static analysis.
    """
    parsed = response.parsed
    if not isinstance(parsed, model_type):
        raise ValueError(
            f"Expected {model_type.__name__} from Gemini, got {type(parsed).__name__}. "
            f"Raw: {response.text}"
        )
    return parsed