import quip
import os
from markdownify import markdownify as md


# Get token from environment variables
base_url = os.environ.get('QUIP_BASE_URL', "https://platform.quip-amazon.com")

def init_quip_client(access_token):
    """Initialize and return a Quip client"""
    
    print('QUIP_ACCESS_TOKEN', access_token)
    if not access_token or not base_url:
        raise ValueError("QUIP_ACCESS_TOKEN environment variable is required")

    client = quip.QuipClient(
        access_token=access_token,
        base_url=base_url,
        request_timeout=20  # Increased timeout
    )

    # Verify authentication
    try:
        user = client.get_authenticated_user()
        print(f"Successfully authenticated as user: {user.get('name', 'Unknown')}")
    except Exception as e:
        print(f"Authentication failed: {str(e)}")
        raise

    return client


def get_document_content(thread_id,access_token):
    try:
        client = init_quip_client(access_token)
        doc = client.get_thread(thread_id)
        thread = doc["thread"]
        content = {
            "thread_id": thread["id"], 
            "thread_type": thread["type"],
            "thread_title": thread["title"],
            "thread_html_content": md(doc['html'], heading_style="ATX")
            }
        return str(content)
    except Exception as e:
        raise


async def get_thread_metadata_impl(thread_id,access_token):
    """Get metadata of a quip thread including its id, type, title and link, by specifying the thread id..
    
    Args:
        thread_id: an unique id of a thread in quip, used to access a quip thread. For example, if a url of a quip thread is https://quip-amazon.com/AbcCFHsxcstk/AgentTest, then the thread id is AbcCFHsxcstk.
    
    Returns:
        String of a python dict, containing the metadata of a quip thread including id, type, title and link.
    """
    try:
        # Get thread data
        client = init_quip_client(access_token)
        thread = client.get_thread(thread_id)["thread"]
        meta_data = {
            "thread_id": thread["id"], 
            "thread_type": thread["type"],
            "thread_title": thread["title"],
            "thread_link": thread["link"]
            }
        return str(meta_data)
    except Exception as e:
        raise


async def get_thread_content_impl(thread_id,access_token):
    """Get content of a quip thread by specifying the thread id.
    
    Args:
        thread_id: an unique id of a thread in quip, used to access a quip thread. For example, if a url of a quip thread is https://quip-amazon.com/AbcCFHsxcstk/AgentTest, then the thread id is AbcCFHsxcstk.
        thread_type: the type of the quip thread. The value is either document or spreadsheet.
    
    Returns:
        Content of the quip thread.
    """
    return get_document_content(thread_id,access_token)

