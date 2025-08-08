from retriever import search_with_threshold
from text_processing import substitute_template, parse_model_response
from config import MODEL, SYSTEMPROMPT, MODEL_URL, OPENAI_API, CLAUDE_API
from openai import OpenAI, OpenAIError, ConflictError, NotFoundError, APIStatusError, RateLimitError, APITimeoutError, BadRequestError, APIConnectionError, AuthenticationError, InternalServerError, PermissionDeniedError, LengthFinishReasonError, UnprocessableEntityError, APIResponseValidationError, ContentFilterFinishReasonError, _AmbiguousModuleClientUsageError
from database import table
from logger import logger
from anthropic import Anthropic

import json
import re

client_openai = OpenAI(api_key=OPENAI_API, base_url=MODEL_URL)
client_anthropic = Anthropic(api_key=CLAUDE_API)  # Add your API key here

def extract_quoted_content(text, start_pos):
    """Extract content from a quoted string, handling escaped quotes"""
    content = ""
    i = start_pos
    
    while i < len(text):
        char = text[i]
        
        # Check for closing quote (not escaped)
        if char == '"':
            # Check if it's escaped
            escape_count = 0
            j = i - 1
            while j >= 0 and text[j] == '\\':
                escape_count += 1
                j -= 1
            
            # If even number of backslashes (or zero), quote is not escaped
            if escape_count % 2 == 0:
                break
        
        # Check for potential end of speech section
        if char == ',' and i + 1 < len(text):
            # Look ahead for "key_themes" or similar
            remaining = text[i:i+50]
            if '"key_themes"' in remaining or '"sentiment"' in remaining:
                # We've likely reached the end of the speech content
                break
        
        content += char
        i += 1
    
    # Clean up the content
    content = content.replace('\\"', '"')  # Unescape quotes
    content = content.replace('\\n', '\n')  # Unescape newlines
    content = content.replace('\\\\', '\\')  # Unescape backslashes
    
    return content

def extract_json_manually(text):
    """Extract JSON components manually when direct parsing fails"""
    
    # Initialize result
    result = {
        "speech": "",
        "key_themes": [],
        "sentiment": {"category": "positive", "explanation": "Default sentiment"}
    }
    
    try:
        # Extract speech content
        speech_start = text.find('"speech":')
        if speech_start != -1:
            # Find the opening quote after "speech":
            quote_start = text.find('"', speech_start + 9)
            if quote_start != -1:
                speech_content = extract_quoted_content(text, quote_start + 1)
                result["speech"] = speech_content
        
        # Extract key_themes array
        themes_start = text.find('"key_themes":')
        if themes_start != -1:
            themes_section = text[themes_start:]
            array_start = themes_section.find('[')
            array_end = themes_section.find(']')
            if array_start != -1 and array_end != -1:
                themes_text = themes_section[array_start+1:array_end]
                # Extract individual theme strings
                themes = []
                for match in re.finditer(r'"([^"]*)"', themes_text):
                    themes.append(match.group(1))
                result["key_themes"] = themes
        
        # Extract sentiment object
        sentiment_start = text.find('"sentiment":')
        if sentiment_start != -1:
            sentiment_section = text[sentiment_start:]
            obj_start = sentiment_section.find('{')
            if obj_start != -1:
                # Find matching closing brace
                brace_count = 0
                obj_end = obj_start
                for i, char in enumerate(sentiment_section[obj_start:]):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            obj_end = obj_start + i
                            break
                
                sentiment_text = sentiment_section[obj_start:obj_end+1]
                try:
                    sentiment_obj = json.loads(sentiment_text)
                    result["sentiment"] = sentiment_obj
                except:
                    # Manual extraction for sentiment
                    category_match = re.search(r'"category":\s*"([^"]*)"', sentiment_text)
                    explanation_match = re.search(r'"explanation":\s*"([^"]*)"', sentiment_text)
                    
                    if category_match:
                        result["sentiment"]["category"] = category_match.group(1)
                    if explanation_match:
                        result["sentiment"]["explanation"] = explanation_match.group(1)
        
        logger.info("Successfully extracted JSON manually")
        return result
        
    except Exception as e:
        logger.error(f"Manual extraction failed: {e}")
        return create_fallback_response("")

def create_fallback_response(raw_text):
    """Create a fallback response when all parsing fails"""
    logger.warning("Using fallback response due to parsing failure")
    
    # Try to extract at least some speech content
    speech_content = "Unable to generate complete speech due to parsing error."
    
    # Simple regex to find any substantial text content
    if raw_text:
        # Look for long text blocks that might be speech content
        text_blocks = re.findall(r'[A-Z][^.!?]*[.!?](?:\s+[A-Z][^.!?]*[.!?])*', raw_text)
        if text_blocks:
            # Take the longest block
            speech_content = max(text_blocks, key=len)
    
    return {
        "speech": speech_content,
        "key_themes": [
            "Vision and Development", 
            "Technology and Innovation", 
            "Economic Growth", 
            "Youth Empowerment", 
            "Agricultural Prosperity"
        ],
        "sentiment": {
            "category": "positive",
            "explanation": "Inspirational political speech with development focus"
        }
    }

def parse_claude_response(response):
    """Robust parser for Claude responses with multiple fallback strategies"""
    try:
        # Step 1: Get raw text from Claude response
        raw_text = response.content[0].text
        logger.debug(f"Raw Claude response length: {len(raw_text)}")
        
        # Step 2: Remove markdown code block formatting
        cleaned_text = raw_text.strip()
        
        # Remove markdown indicators
        if cleaned_text.startswith('```json'):
            cleaned_text = cleaned_text[7:]
        elif cleaned_text.startswith('```'):
            cleaned_text = cleaned_text[3:]
        elif cleaned_text.startswith('json'):
            cleaned_text = cleaned_text[4:]
        
        if cleaned_text.endswith('```'):
            cleaned_text = cleaned_text[:-3]
        
        cleaned_text = cleaned_text.strip()
        
        # Step 3: Try direct JSON parsing first
        try:
            logger.info("Attempting direct JSON parsing")
            return json.loads(cleaned_text)
        except json.JSONDecodeError as e:
            logger.warning(f"Direct JSON parsing failed: {e}, attempting manual extraction")
            pass
        
        # Step 4: Try simple cleanup and parse again
        try:
            # Remove everything before first { and after last }
            start = cleaned_text.find('{')
            end = cleaned_text.rfind('}') + 1
            if start != -1 and end != 0:
                json_section = cleaned_text[start:end]
                logger.info("Attempting parsing with bracket extraction")
                return json.loads(json_section)
        except json.JSONDecodeError as e:
            logger.warning(f"Bracket extraction parsing failed: {e}")
            pass
        
        # Step 5: Manual extraction approach
        logger.info("Attempting manual component extraction")
        return extract_json_manually(cleaned_text)
        
    except Exception as e:
        logger.error(f"Complete parsing failure: {e}")
        return create_fallback_response(raw_text if 'raw_text' in locals() else "")

def generate_response(data: dict):
    if not isinstance(data, dict):
        logger.error("Invalid input: data must be a dictionary")
        return {"error": "ERR_INVALID_INPUT", "message": "Input data must be a dictionary"}

    if data.get("political-party") == "other":
        data["political-party"] = data.get("other-party", "")
        if not data["political-party"]:
            logger.warning("Other party selected but no party name provided")

    # Ensure emotional-appeal has a default value if not provided
    if "emotional-appeal" not in data or not data["emotional-appeal"]:
        data["emotional-appeal"] = "Neutral"
        
    # Ensure humor has a default value if not provided
    if "humor" not in data or not data["humor"]:
        data["humor"] = "Balanced"

    # Check if RAG should be enabled
    enable_rag = data.get("enable_rag", True)  # Default to True if not specified
    
    if enable_rag:
        # Only perform RAG if explicitly enabled
        q1 = data.get("candidate-name", "")
        if not q1:
            logger.error("Missing required field: candidate-name")
            return {"error": "ERR_MISSING_FIELD", "message": "Candidate name is required"}

        q3 = data.get("political-party", "")
        if not q3:
            logger.error("Missing required field: political-party")
            return {"error": "ERR_MISSING_FIELD", "message": "Political party is required"}

        query = f"{q1} {q3}"
        logger.info(f"Performing search with the query : {query}")
        
        try:
            data["retrieved_info"] = search_with_threshold(table, query, threshold=0.75)
            logger.info("Retrieved information successfully")
        except Exception as e:
            logger.error(f"Search operation failed: {e}")
            return {"error": "ERR_SEARCH_FAILED", "message": f"Failed to retrieve information: {str(e)}"}
    else:
        # If RAG is disabled, set retrieved_info to empty string
        logger.info("RAG is disabled. Skipping retrieval step.")
        data["retrieved_info"] = ""

    try:
        formatted_prompt = substitute_template(data)
        logger.info("Formatted prompt successfully")
        logger.debug(f"Full Prompt:\n {formatted_prompt}")
    except Exception as e:
        logger.error(f"Template substitution failed: {e}")
        return {"error": "ERR_TEMPLATE_FAILED", "message": f"Failed to format prompt: {str(e)}"}

    message_list = [
        {
            "role": "user",
            "content": [
                {"type":"text","text": formatted_prompt},
            ]
        }
    ]

    # Enhanced system prompt for better JSON formatting
    enhanced_system_prompt = SYSTEMPROMPT + """

CRITICAL JSON FORMATTING RULES:
1. Return ONLY valid JSON - no markdown, no code blocks, no extra text
2. Start with { and end with }
3. Escape all quotes in speech content with \"
4. Replace all line breaks in speech with \\n
5. No unescaped quotes or control characters in any string values
6. Double-check JSON validity before responding"""

    try: 
        logger.info("Calling Claude for response generation")
        response = client_anthropic.messages.create(
            system=enhanced_system_prompt,
            model="claude-opus-4-1-20250805",
            max_tokens=4096,
            messages=message_list
        )
        logger.info("Successfully received response from Claude API")
    except RateLimitError as e:
        logger.error(f"Rate limit exceeded: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Rate limit exceeded. Please try again later: {str(e)}"}
    except APITimeoutError as e:
        logger.error(f"API request timed out: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Request to Claude API timed out: {str(e)}"}
    except APIConnectionError as e:
        logger.error(f"Connection error with Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Failed to connect to Claude API. Check your network connection: {str(e)}"}
    except AuthenticationError as e:
        logger.error(f"Authentication error with Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Authentication failed. Check your API key: {str(e)}"}
    except PermissionDeniedError as e:
        logger.error(f"Permission denied by Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Permission denied to access the requested resource: {str(e)}"}
    except BadRequestError as e:
        logger.error(f"Bad request to Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Invalid request parameters sent to Claude API: {str(e)}"}
    except NotFoundError as e:
        logger.error(f"Resource not found in Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"The requested resource was not found. Check model name: {str(e)}"}
    except ConflictError as e:
        logger.error(f"Conflict error with Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Request conflicts with current state of the server: {str(e)}"}
    except InternalServerError as e:
        logger.error(f"Internal server error from Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Claude API experienced an internal error. Try again later: {str(e)}"}
    except UnprocessableEntityError as e:
        logger.error(f"Unprocessable entity error from Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"The request was well-formed but unable to be processed: {str(e)}"}
    except ContentFilterFinishReasonError as e:
        logger.error(f"Content filter triggered in Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Response was filtered due to content safety policies: {str(e)}"}
    except LengthFinishReasonError as e:
        logger.error(f"Response length limit reached in Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Response was truncated due to token limit constraints: {str(e)}"}
    except APIResponseValidationError as e:
        logger.error(f"API response validation error from Claude: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Claude API response failed validation: {str(e)}"}
    except APIStatusError as e:
        logger.error(f"API status error from Claude: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Claude API returned an unexpected status code: {str(e)}"}
    except _AmbiguousModuleClientUsageError as e:
        logger.error(f"Ambiguous module client usage with Claude API: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Ambiguous usage of Claude client: {str(e)}"}
    except OpenAIError as e:
        logger.error(f"General Claude API error: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"An error occurred with the Claude API: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error occurred while generating response: {e}")
        return {"error": "ERR_API_FAILURE", "message": f"Failed to get response from Claude API: {str(e)}"}

    # Use the robust parser
    return parse_claude_response(response)

def generate_extended_speech(data: dict, target_word_count: int = 2500, max_attempts: int = 15) -> dict:
    total_speech = ""
    full_response = {}
    attempt = 0

    enrichment_strategies = [
        "Add emotional appeals and empathy for affected people.",
        "Include historical achievements and visionary goals.",
        "Incorporate recent development examples.",
        "Add quotes, stories or testimonies from citizens.",
        "End with a powerful call to action."
    ]

    while len(total_speech.split()) < target_word_count and attempt < max_attempts:
        logger.info(f"♻ Attempt {attempt + 1}: Generating more speech... Current word count: {len(total_speech.split())}")

        enriched_data = data.copy()
        if attempt < len(enrichment_strategies):
            enriched_data["extra-context"] = enrichment_strategies[attempt]
        else:
            enriched_data["extra-context"] = "Expand more deeply on different angles of the topic."

        response = generate_response(enriched_data)

        if isinstance(response, dict) and "speech" in response:
            new_speech = response["speech"].strip()

            if new_speech:
                total_speech += "\n\n" + new_speech
                full_response = response
            else:
                logger.warning("⚠️ Empty or duplicate speech chunk detected. Skipping...")
        else:
            logger.error("❌ Invalid response or no 'speech' key present.")
            break

        attempt += 1

    if not total_speech:
        logger.warning("❗ No valid speech chunks were generated.")
        total_speech = "Unable to generate a speech."

    full_response["speech"] = total_speech
    logger.info(f"✅ Final speech length: {len(total_speech.split())} words across {attempt} attempts")
    return full_response
