import atexit
import json
import multiprocessing
import time
import uuid

# HF Spaces compatibility - browsergym is optional
BROWSERGYM_AVAILABLE = False
gym = None
flatten_dom_to_str = None
overlay_som = None

try:
    import gymnasium as gym
    import browsergym.core  # noqa F401 (we register the openended task as a gym environment)
    from browsergym.utils.obs import flatten_dom_to_str, overlay_som
    BROWSERGYM_AVAILABLE = True
except ImportError as e:
    # Fallback functions for when browsergym is not available
    def flatten_dom_to_str(*args, **kwargs):
        return "BrowserGym not available"
    def overlay_som(*args, **kwargs):
        return None
    # Dummy gym for compatibility
    class gym:
        @staticmethod
        def make(*args, **kwargs):
            raise ImportError("BrowserGym not available")

import html2text
import tenacity

from openhands.core.exceptions import BrowserInitException
from openhands.core.logger import openhands_logger as logger
from openhands.runtime.browser.base64 import image_to_png_base64_url
from openhands.utils.shutdown_listener import should_continue, should_exit
from openhands.utils.tenacity_stop import stop_if_should_exit

BROWSER_EVAL_GET_GOAL_ACTION = 'GET_EVAL_GOAL'
BROWSER_EVAL_GET_REWARDS_ACTION = 'GET_EVAL_REWARDS'


class BrowserEnv:
    def __init__(self, browsergym_eval_env: str | None = None):
        self.html_text_converter = self.get_html_text_converter()
        self.eval_mode = False
        self.eval_dir = ''

        # EVAL only: browsergym_eval_env must be provided for evaluation
        self.browsergym_eval_env = browsergym_eval_env
        self.eval_mode = bool(browsergym_eval_env) and BROWSERGYM_AVAILABLE
        
        if browsergym_eval_env and not BROWSERGYM_AVAILABLE:
            logger.warning("BrowserGym evaluation requested but browsergym not available. Disabling eval mode.")

        # Initialize browser environment process only if browsergym is available
        if BROWSERGYM_AVAILABLE:
            multiprocessing.set_start_method('spawn', force=True)
            self.browser_side, self.agent_side = multiprocessing.Pipe()
            self.init_browser()
            atexit.register(self.close)
        else:
            logger.warning("BrowserGym not available. Browser functionality disabled.")

    def get_html_text_converter(self) -> html2text.HTML2Text:
        html_text_converter = html2text.HTML2Text()
        # ignore links and images
        html_text_converter.ignore_links = False
        html_text_converter.ignore_images = True
        # use alt text for images
        html_text_converter.images_to_alt = True
        # disable auto text wrapping
        html_text_converter.body_width = 0
        return html_text_converter

    @tenacity.retry(
        wait=tenacity.wait_fixed(1),
        stop=tenacity.stop_after_attempt(5) | stop_if_should_exit(),
        retry=tenacity.retry_if_exception_type(BrowserInitException),
    )
    def init_browser(self) -> None:
        logger.debug('Starting browser env...')
        try:
            self.process = multiprocessing.Process(target=self.browser_process)
            self.process.start()
        except Exception as e:
            logger.error(f'Failed to start browser process: {e}')
            raise

        if not self.check_alive(timeout=200):
            self.close()
            raise BrowserInitException('Failed to start browser environment.')

    def browser_process(self) -> None:
        if not BROWSERGYM_AVAILABLE:
            logger.error("BrowserGym not available. Cannot start browser process.")
            return
            
        if self.eval_mode:
            assert self.browsergym_eval_env is not None
            logger.info('Initializing browser env for web browsing evaluation.')
            if not self.browsergym_eval_env.startswith('browsergym/'):
                self.browsergym_eval_env = 'browsergym/' + self.browsergym_eval_env
            try:
                if 'visualwebarena' in self.browsergym_eval_env:
                    import browsergym.visualwebarena  # noqa F401 register visualwebarena tasks as gym environments
                    import nltk
                    nltk.download('punkt_tab')
                elif 'webarena' in self.browsergym_eval_env:
                    import browsergym.webarena  # noqa F401 register webarena tasks as gym environments
                elif 'miniwob' in self.browsergym_eval_env:
                    import browsergym.miniwob  # noqa F401 register miniwob tasks as gym environments
                else:
                    raise ValueError(
                        f'Unsupported browsergym eval env: {self.browsergym_eval_env}'
                    )
                env = gym.make(self.browsergym_eval_env, tags_to_mark='all', timeout=100000)
            except ImportError as e:
                logger.error(f"Failed to import browsergym environment: {e}")
                return
        else:
            try:
                env = gym.make(
                    'browsergym/openended',
                    task_kwargs={'start_url': 'about:blank', 'goal': 'PLACEHOLDER_GOAL'},
                    wait_for_user_message=False,
                    headless=True,
                    disable_env_checker=True,
                    tags_to_mark='all',
                )
            except Exception as e:
                logger.error(f"Failed to create browsergym environment: {e}")
                return
        obs, info = env.reset()

        logger.info('Successfully called env.reset')
        # EVAL ONLY: save the goal into file for evaluation
        self.eval_goal = None
        self.goal_image_urls = []
        self.eval_rewards: list[float] = []
        if self.eval_mode:
            self.eval_goal = obs['goal']
            if 'goal_object' in obs:
                if len(obs['goal_object']) > 0:
                    self.eval_goal = obs['goal_object'][0]['text']
                for message in obs['goal_object']:
                    if message['type'] == 'image_url':
                        image_src = message['image_url']
                        if isinstance(image_src, dict):
                            image_src = image_src['url']
                        self.goal_image_urls.append(image_src)
            logger.debug(f'Browsing goal: {self.eval_goal}')
        logger.info('Browser env started.')

        while should_continue():
            try:
                if self.browser_side.poll(timeout=0.01):
                    unique_request_id, action_data = self.browser_side.recv()

                    # shutdown the browser environment
                    if unique_request_id == 'SHUTDOWN':
                        logger.debug('SHUTDOWN recv, shutting down browser env...')
                        env.close()
                        return
                    elif unique_request_id == 'IS_ALIVE':
                        self.browser_side.send(('ALIVE', None))
                        continue

                    # EVAL ONLY: Get evaluation info
                    if action_data['action'] == BROWSER_EVAL_GET_GOAL_ACTION:
                        self.browser_side.send(
                            (
                                unique_request_id,
                                {
                                    'text_content': self.eval_goal,
                                    'image_content': self.goal_image_urls,
                                },
                            )
                        )
                        continue
                    elif action_data['action'] == BROWSER_EVAL_GET_REWARDS_ACTION:
                        self.browser_side.send(
                            (
                                unique_request_id,
                                {'text_content': json.dumps(self.eval_rewards)},
                            )
                        )
                        continue

                    action = action_data['action']
                    obs, reward, terminated, truncated, info = env.step(action)

                    # EVAL ONLY: Save the rewards into file for evaluation
                    if self.eval_mode:
                        self.eval_rewards.append(reward)

                    # add text content of the page
                    html_str = flatten_dom_to_str(obs['dom_object'])
                    obs['text_content'] = self.html_text_converter.handle(html_str)
                    # make observation serializable
                    obs['set_of_marks'] = image_to_png_base64_url(
                        overlay_som(
                            obs['screenshot'], obs.get('extra_element_properties', {})
                        ),
                        add_data_prefix=True,
                    )
                    obs['screenshot'] = image_to_png_base64_url(
                        obs['screenshot'], add_data_prefix=True
                    )
                    obs['active_page_index'] = obs['active_page_index'].item()
                    obs['elapsed_time'] = obs['elapsed_time'].item()
                    self.browser_side.send((unique_request_id, obs))
            except KeyboardInterrupt:
                logger.debug('Browser env process interrupted by user.')
                try:
                    env.close()
                except Exception:
                    pass
                return

    def step(self, action_str: str, timeout: float = 100) -> dict:
        """Execute an action in the browser environment and return the observation."""
        unique_request_id = str(uuid.uuid4())
        self.agent_side.send((unique_request_id, {'action': action_str}))
        start_time = time.time()
        while True:
            if should_exit() or time.time() - start_time > timeout:
                raise TimeoutError('Browser environment took too long to respond.')
            if self.agent_side.poll(timeout=0.01):
                response_id, obs = self.agent_side.recv()
                if response_id == unique_request_id:
                    return dict(obs)

    def check_alive(self, timeout: float = 60) -> bool:
        self.agent_side.send(('IS_ALIVE', None))
        if self.agent_side.poll(timeout=timeout):
            response_id, _ = self.agent_side.recv()
            if response_id == 'ALIVE':
                return True
            logger.debug(f'Browser env is not alive. Response ID: {response_id}')
        return False

    def close(self) -> None:
        if not self.process.is_alive():
            return
        try:
            self.agent_side.send(('SHUTDOWN', None))
            self.process.join(5)  # Wait for the process to terminate
            if self.process.is_alive():
                logger.error(
                    'Browser process did not terminate, forcefully terminating...'
                )
                self.process.terminate()
                self.process.join(5)  # Wait for the process to terminate
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(5)  # Wait for the process to terminate
            self.agent_side.close()
            self.browser_side.close()
        except Exception as e:
            logger.error(f'Encountered an error when closing browser env: {e}')
