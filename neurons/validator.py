# Import necessary libraries and modules
import os
import time
import argparse
import traceback
import bittensor as bt
import torch.multiprocessing as mp
import torch
from typing import Tuple
import mapreduce
import mapreduce.utils as utils
from dist_validator import start_master_process
from threading import Thread
import json

def get_validator_config_from_json():
    """
    Reads the validator configuration from a JSON file.
    
    Returns:
        validator_config (dict): Configuration parameters read from the JSON file.
    """
    with open('validator.config.json') as f:
        validator_config = json.load(f)
    return validator_config

# Load the validator configuration from the JSON file
validator_config = get_validator_config_from_json()

def get_config():
    """
    Sets up the configuration parser and initializes necessary command-line arguments.
    
    Returns:
        config (Namespace): Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser()
    # Adds override arguments for network and netuid.
    parser.add_argument( '--subtensor.network', default = validator_config['subtensor.network'], help = "The subtensor network." )
    parser.add_argument( '--netuid', type = int, default = validator_config['netuid'], help = "The chain subnet uid." )
    parser.add_argument( '--wallet.name', default = validator_config['wallet.name'], help = "Wallet name" )
    parser.add_argument( '--wallet.hotkey', default = validator_config['wallet.hotkey'], help = "Wallet hotkey" )
    # Adds subtensor specific arguments i.e. --subtensor.chain_endpoint ... --subtensor.network ...
    bt.subtensor.add_args(parser)
    # Adds logging specific arguments i.e. --logging.debug ..., --logging.trace .. or --logging.logging_dir ...
    bt.logging.add_args(parser)
    # Adds wallet specific arguments i.e. --wallet.name ..., --wallet.hotkey ./. or --wallet.path ...
    bt.wallet.add_args(parser)
    # Adds axon specific arguments i.e. --axon.port ...
    bt.axon.add_args(parser)
    # Activating the parser to read any command-line inputs.
    config = bt.config(parser)

    # Step 3: Set up logging directory
    # Logging captures events for diagnosis or understanding validator's behavior.
    config.full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            'validator',
        )
    )
    # Ensure the directory for logging exists, else create one.
    if not os.path.exists(config.full_path): os.makedirs(config.full_path, exist_ok=True)
    return config


# Global variable to store process information
processes = {}

# Global variable to store miner status
miner_status = []
    
# Main takes the config and starts the validator.
def main( config ):

    # Activating Bittensor's logging with the set configurations.
    bt.logging(config=config, logging_dir=config.full_path)
    bt.logging.info(f"Running validator for subnet: {config.netuid} on network: {config.subtensor.chain_endpoint} with config:")

    # This logs the active configuration to the specified logging directory for review.
    bt.logging.info(config)

    # Step 4: Initialize Bittensor validator objects
    # These classes are vital to interact and function within the Bittensor network.
    bt.logging.info("Setting up bittensor objects.")

    # Wallet holds cryptographic information, ensuring secure transactions and communication.
    wallet = bt.wallet( config = config )
    bt.logging.info(f"Wallet: {wallet}")

    # subtensor manages the blockchain connection, facilitating interaction with the Bittensor blockchain.
    subtensor = bt.subtensor( config = config )
    bt.logging.info(f"Subtensor: {subtensor}")
    
    # Dendrite is the RPC client; it lets us send messages to other nodes (axons) in the network.
    dendrite = bt.dendrite( wallet = wallet )

    # metagraph provides the network's current state, holding state about other participants in a subnet.
    metagraph = subtensor.metagraph(config.netuid)
    bt.logging.info(f"Metagraph: {metagraph} {metagraph.axons}")
    
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        bt.logging.error(f"\nYour validator: {wallet} if not registered to chain connection: {subtensor} \nRun btcli register and try again. ")
        exit()
    else:
        # Each validator gets a unique identity (UID) in the network for differentiation.
        my_subnet_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
        bt.logging.info(f"Running validator on uid: {my_subnet_uid}")

    # Step 5: Build and link validator functions to the axon.
    # The axon handles request processing, allowing validators to send this process requests.
    axon = bt.axon( wallet = wallet, config = config, port = config.axon.port )
    bt.logging.info(f"Axon {axon}")

    def calculate_score():
        scores = torch.zeros_like(metagraph.S, dtype=torch.float32)
        speed_scores = torch.zeros_like(metagraph.S, dtype=torch.float32)
        bandwidth_scores = torch.zeros_like(metagraph.S, dtype=torch.float32)
        ip_count = {}
        for miner in miner_status:
            if miner['status'] == 'benchmarked':
                uid = miner['uid']
                speed_scores[uid] = miner['speed']
                bandwidth_scores[uid] = miner['bandwidth']
                ip = metagraph.neurons[uid].axon_info.ip
                ip_count[ip] = ip_count.get(ip, 0) + 1
        
        # Divide by ip count
        for miner in miner_status:
            if miner['status'] == 'benchmarked':
                ip = metagraph.neurons[uid].axon_info.ip
                speed_scores[uid] = speed_scores[uid] / ip_count[ip]
                bandwidth_scores[uid] = bandwidth_scores[uid] / ip_count[ip]
        
        speed_scores = torch.nn.functional.normalize(speed_scores, p=1.0, dim=0)
        bandwidth_scores = torch.nn.functional.normalize(bandwidth_scores, p=1.0, dim=0)
        scores = speed_scores * 0.5 + bandwidth_scores * 0.5
        return scores
    
    def init_miner_status():
        if len(miner_status) == 0:
            for uid, hotkey in enumerate(metagraph.hotkeys):
                miner_status.append({
                    'uid': uid,
                    'hotkey': hotkey,
                    'free_memory': 0,
                    'bandwidth': 0,
                    'speed': 0,
                    'status': 'unavailable',
                    'timestamp': 0,
                })
        else:
            for uid, hotkey in enumerate(metagraph.hotkeys):
                if miner_status[uid]['status'] in ['available', 'benchmarked']:
                    miner_status[uid]['status'] = 'available'
                else:
                    miner_status[uid]['status'] = 'unavailable'
                    miner_status[uid]['speed'] = 0
                    miner_status[uid]['bandwidth'] = 0
                    miner_status[uid]['bandwidth_updated_at'] = 0
        
    # Choose miner to benchmark
    def choose_miner():
        available_miners = [miner for miner in miner_status if miner['status'] == 'available']
        if len(available_miners) == 0:
            return None
        # choose an available miner randomly
        return available_miners[ int(torch.randint(0, len(available_miners), (1,)))]
        
    def update_miner_status():
        responses = dendrite.query(metagraph.axons, mapreduce.protocol.MinerStatus(
            version=utils.get_my_version(),
        ))
        for response, miner in zip(responses, miner_status):
            if response.available and ( miner['status'] == 'unavailable' or miner['status'] == 'available'):
                miner['status'] = 'available'
                miner['timestamp'] = time.time()
                miner['free_memory'] = response.free_memory
        bt.logging.info(f"available miners to benchmark: {[miner['uid'] for miner in miner_status if miner['status'] == 'available']}")
    
    def wait_for_benchmark_result(hotkey, miner_uid):
        bt.logging.info(f"⌛ Waiting for benchmark result {miner_uid} {hotkey}")
        start_at = time.time()
        while True:
            if hotkey not in processes or not processes[hotkey]['process'].is_alive():
                bt.logging.info(f"❌ Master process died")
                return
            if processes[hotkey]['output_queue'].empty():
                time.sleep(1)
                if time.time() - start_at > 3600:
                    bt.logging.error(f'Timeout while waiting for benchmark result {miner_uid}, exiting ...')
                    break
                continue
            result:mapreduce.protocol.BenchmarkResult = processes[hotkey]['output_queue'].get()
            log =  (f'🟢 Benchmark Result | UID: {miner_uid} | '\
                    f'Duration: {result.duration:.2f}s | '\
                    f'Data: {utils.human_readable_size(result.data_length)} | '\
                    f'Bandwidth: {utils.human_readable_size(result.bandwidth)} | '\
                    f'Speed: {utils.human_readable_size(result.speed)}/s | '\
            )
            bt.logging.success(log)
            miner_status[miner_uid]['status'] = 'benchmarked'
            miner_status[miner_uid]['speed'] = result.speed
            miner_status[miner_uid]['timestamp'] = time.time()
            miner_status[miner_uid]['bandwidth'] = result.bandwidth
            miner_status[miner_uid]['bandwidth_updated_at'] = time.time()
            processes[hotkey]['input_queue'].put('exit')
            break
    
    """
    Process benchmark request
    """
    def request_benchmark( synapse: mapreduce.protocol.RequestBenchmark ) -> mapreduce.protocol.RequestBenchmark:
        hotkey = synapse.dendrite.hotkey
        bt.logging.info(f"Benchmark request from {hotkey}")
                
        # Version checking
        if not utils.check_version(synapse.version):
            synapse.version = utils.get_my_version()
            return synapse
        
        # Choose un-benchmarked miner
        miner = choose_miner()
        
        if miner is None:
            bt.logging.info(f"No miner to benchmark")
            synapse.miner_uid = -1
            return synapse
        else:
            bt.logging.info(f"Benchmarking Miner UID: {miner['uid']}")
        
        # Set miner_uid
        synapse.miner_uid = miner['uid']
        
        # Query the miner status and create job for the bot
        try:
            synapse.job.rank = 1
            synapse.job.world_size = 3
            if hotkey in processes and processes[hotkey]['process'].is_alive():
                bt.logging.error(f'Master process running {synapse.miner_uid} {hotkey}')
                traceback.print_exc()
                synapse.job.status = 'error'
                synapse.job.reason = 'Master process running'
                return synapse
            # try to join the group
            synapse.job.master_hotkey = axon.wallet.hotkey.ss58_address
            synapse.job.client_hotkey = hotkey
            synapse.job.master_addr = axon.external_ip
            synapse.job.master_port = utils.get_unused_port(9000, 9300)
            
            miner_bandwidth = miner.get('bandwidth', 0)
            current_bandwidth = utils.calc_bandwidth_from_memory(miner.get('free_memory',0))
            # If the miner got bandwidth benchmark already, use 100 MB bandwidth for benchmarking
            # Bandwidth are benchmarked every 6 hours
            if miner_bandwidth > 0:
                if (time.time() - miner.get('bandwidth_updated_at', 0) < 6 * 3600) and miner_bandwidth == validator_config["max_bandwidth"] or current_bandwidth > miner_bandwidth:
                    synapse.job.bandwidth = 100 * 1024 * 1024
            synapse.job.bandwidth = min(current_bandwidth, synapse.job.bandwidth)
            
            bt.logging.info("⌛ Starting benchmarking process")
            bt.logging.trace(synapse.job)
            
            # input, output queues for communication between the main process and dist_validator process
            input_queue = mp.Queue()
            output_queue = mp.Queue()
            synapse.job.status = "init"
            miners = [(synapse.miner_uid, metagraph.axons[synapse.miner_uid])]
            process = mp.Process(target=start_master_process, args=(input_queue, output_queue, wallet, miners, synapse.job, True))
            process.start()
            job : mapreduce.protocol.Job = output_queue.get()
            bt.logging.info("Master process running")
            # store process information
            processes[hotkey] = {
                'process': process,
                'input_queue': input_queue,
                'output_queue': output_queue,
                'type': 'master',
                'job': job,
                'benchmarking': True,
                'miners': miners
            }
            synapse.job = processes[hotkey]['job']
            synapse.job.rank = 1
            bt.logging.trace(synapse.job)
            # create thread for waiting benchmark result
            thread = Thread(target=wait_for_benchmark_result, args=(hotkey, synapse.miner_uid, ))
            thread.start()
            return synapse
        except Exception as e:
            # if failed, set joining to false
            bt.logging.error(f"❌ {e}")
            traceback.print_exc()
            synapse.job.reason = str(e)
            del processes[hotkey]
            miner['status'] = 'available'
            return synapse

    def blacklist_request_benchmark(synapse: mapreduce.protocol.RequestBenchmark) -> Tuple[bool, str]:
        hotkey = synapse.dendrite.hotkey
        # Check if the hotkey is benchmark_hotkey
        if hotkey not in validator_config['benchmark_hotkeys']:
            bt.logging.error(f"Hotkey {hotkey} is not benchmark_hotkey")
            synapse.job.reason = f"Hotkey {hotkey} is not benchmark_hotkey"
            return True, ""
        return False, ""

    def connect_master( synapse: mapreduce.protocol.ConnectMaster ) -> mapreduce.protocol.ConnectMaster:
        hotkey = synapse.dendrite.hotkey
        bt.logging.info(f"Connecting request from {hotkey}")
        
        # Version checking
        if not utils.check_version(synapse.version):
            synapse.version = utils.get_my_version()
            return synapse
        
        try:
            rank = synapse.job.rank
            if synapse.job.rank == 1:
                if hotkey in processes and processes[hotkey]['process'].is_alive():
                    traceback.print_exc()
                    synapse.job.status = 'error'
                    synapse.job.reason = 'Master process running'
                    return synapse
                # try to join the group
                synapse.job.master_hotkey = axon.wallet.hotkey.ss58_address
                synapse.job.client_hotkey = hotkey
                synapse.job.master_addr = axon.external_ip
                synapse.job.master_port = utils.get_unused_port(9000, 9300)
                
                bt.logging.info("⌛ Starting work process")
                bt.logging.trace(synapse.job)
                
                input_queue = mp.Queue()
                output_queue = mp.Queue()
                synapse.job.status = "init"
                
                # Choose available miners to join.
                miners = [(int(uid), axon) for uid, axon, miner in zip(metagraph.uids, metagraph.axons, miner_status) if axon.is_serving and ((miner['status'] == 'available' and calculate_bandwidth_from_free_memory(miner.get('free_memory',0)) >= synapse.job.bandwidth) or (miner['status'] == 'benchmarked' and miner['bandwidth'] >= synapse.job.bandwidth))]
                bt.logging.info(f"Available miners to join: {miners}")
                print(wallet, miners, synapse.job)
                process = mp.Process(target=start_master_process, args=(input_queue, output_queue, wallet, miners, synapse.job, False))
                process.start()
                job : mapreduce.protocol.Job = output_queue.get()
                bt.logging.info("Master process running")
                processes[hotkey] = {
                    'process': process,
                    'input_queue': input_queue,
                    'output_queue': output_queue,
                    'type': 'master',
                    'job': job,
                    'benchmarking': False,
                    'retry': 0,
                    'miners': job.miners
                }
                bt.logging.info(f"Connected miners: {job.miners}")
                for uid in job.miners:
                    miner_status[uid]['status'] = 'working'
            else:
                if hotkey not in processes:
                    wait_for_master_process(hotkey)
            synapse.job = processes[hotkey]['job']
            synapse.job.rank = rank
            bt.logging.trace(synapse.job)
            return synapse
        except Exception as e:
            # if failed, set joining to false
            bt.logging.info(f"❌ {e}")
            traceback.print_exc()
            if hotkey in processes and processes[hotkey]['job']:
                for uid in processes[hotkey]['job'].miners:
                    miner_status[uid]['status'] = 'available'
            synapse.job.status = 'error'
            synapse.job.reason = str(e)
            return synapse
    
    # Prepare benchmark result, benchmark bot information 
    def get_benchmark_result( synapse: mapreduce.protocol.BenchmarkResults) -> mapreduce.protocol.BenchmarkResults:
        hotkey = synapse.dendrite.hotkey
        
        bt.logging.info(f"Get benchmark result request from {hotkey}")
        # Version checking
        if not utils.check_version(synapse.version):
            synapse.version = utils.get_my_version()
            return synapse
        # Check if the master process is running
        # Get the result from the master process
        synapse.results = [ mapreduce.protocol.BenchmarkResult(
            duration = miner['duration'],
            data_length = miner['data_length'],
            bandwidth = miner['bandwidth'],
            speed = miner['speed'],
            free_memory = miner['free_memory'],
        ) for miner in miner_status ]
        return synapse

    def blacklist_get_benchmark_result( synapse: mapreduce.protocol.BenchmarkResults ) -> Tuple[bool, str]:
        hotkey = synapse.dendrite.hotkey
        allowed_hotkeys = [
            "5DkRd1V7eurDpgKbiJt7YeJzQvxgpPiiU6FMDf8RmYQ78DpD", # Allow subnet owner's hotkey to fetch benchmark results from validators
        ]
        # Check if the hotkey is allowed list
        if hotkey not in allowed_hotkeys:
            return True, ""
        return False, ""
        
    def log_miner_status():
        for miner in miner_status:
            color = '0' #green
            if miner['status'] == 'benchmarked':
                color = '92'
            if miner['status'] == 'available' or miner['status'] == 'benchmarking' or miner['status'] == 'working':
                color = '94'
            if miner['status'] == 'failed':
                color = '91'
            
            bt.logging.info(f"Miner {miner['uid']} \033[{color}m{miner['status']}\033[0m | \033[{color}m{scores[miner['uid']]}\033[0m | Speed: \033[{color}m{utils.human_readable_size(miner.get('speed', 0))}/s\033[0m | Bandwidth: \033[{color}m{utils.human_readable_size(miner.get('bandwidth', 0))}\033[0m | Free Memory: \033[{color}m{utils.human_readable_size(miner.get('free_memory', 0))}\033[0m")
        

    init_miner_status()
    
    # Attach determiners which functions are called when servicing a request.
    bt.logging.info(f"Attaching forward function to axon.")
    axon.attach(
        forward_fn = connect_master,   
    ).attach(
        forward_fn = request_benchmark,
        blacklist_fn = blacklist_request_benchmark
    ).attach(
        forward_fn = get_benchmark_result,
        blacklist_fn = blacklist_get_benchmark_result
    )


    # Serve passes the axon information to the network + netuid we are hosting on.
    # This will auto-update if the axon port of external ip have changed.
    bt.logging.info(f"Serving axon on network: {config.subtensor.chain_endpoint} with netuid: {config.netuid}")
    axon.serve( netuid = config.netuid, subtensor = subtensor )

    # Start  starts the miner's axon, making it active on the network.
    bt.logging.info(f"Starting axon server on port: {config.axon.port}")
    axon.start()

    # Check processes
    thread = Thread(target=utils.check_processes, args=(processes, miner_status))
    thread.start()

    # Step 6: Keep the miner alive
    # This loop maintains the miner's operations until intentionally stopped.
    bt.logging.info(f"Starting main loop")
    step = 0


    scores = torch.ones_like(metagraph.S, dtype=torch.float32)
    bt.logging.info(f"Weights: {scores}")
    
    last_updated_block = subtensor.block
    
    scores_file = "scores.pt"
    try:
        scores = torch.load(scores_file)
        bt.logging.info(f"Loaded scores from save file: {scores}")
    except:
        scores = torch.zeros_like(metagraph.S, dtype=torch.float32)
        bt.logging.info(f"Initialized all scores to 0")
    
    # set all nodes without ips set to 0
    scores = scores * torch.Tensor([metagraph.neurons[uid].axon_info.ip != '0.0.0.0' for uid in metagraph.uids])
    step = 0

    alpha = 0.7
    while True:
        try:
            # Below: Periodically update our knowledge of the network graph.
            if step % 5 == 0:
                metagraph = subtensor.metagraph(config.netuid)
                update_miner_status()            
                for miner in miner_status:
                    if miner['status'] == 'benchmarked':
                        bt.logging.info(f"Miner {miner['uid']} | Speed: {utils.human_readable_size(miner['speed'])}/s | Bandwidth: {utils.human_readable_size(miner['bandwidth'])}")
            if step % 20 == 0:
                log_miner_status()        
                    
            # Periodically update the weights on the Bittensor blockchain.
            current_block = subtensor.block
            if current_block - last_updated_block > 100:
                
                # Skip setting wait if there are miners benchmarking or not benchmarked yet
                is_benchmarking = False
                for miner in miner_status:
                    if miner['status'] == 'benchmarking' or miner['status'] == 'available':
                        bt.logging.debug("Benchmarking is in progress, skip score calculation")
                        is_benchmarking = True
                        break
                        
                if is_benchmarking:
                    step += 1
                    time.sleep(bt.__blocktime__)
                    continue
                
                bt.logging.success("Updating score ...")                
                new_scores = calculate_score()
                
                scores = new_scores * alpha + scores * (1 - alpha)
                                
                log_miner_status()
                
                weights = torch.nn.functional.normalize(scores, p=1.0, dim=0)
                bt.logging.info(f"Setting weights: {weights}")
                # This is a crucial step that updates the incentive mechanism on the Bittensor blockchain.
                # Miners with higher scores (or weights) receive a larger share of TAO rewards on this subnet.
                result = subtensor.set_weights(
                    netuid = config.netuid, # Subnet to set weights on.
                    wallet = wallet, # Wallet to sign set weights using hotkey.
                    uids = metagraph.uids, # Uids of the miners to set weights for.
                    weights = weights, # Weights to set for the miners. 
                    wait_for_inclusion = True
                )
                last_updated_block = current_block
                if result: 
                    bt.logging.success('✅ Successfully set weights.')
                    init_miner_status()
                else: bt.logging.error('Failed to set weights.')    
            
            step += 1
            time.sleep(bt.__blocktime__)


        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            axon.stop()
            bt.logging.success('Validator killed by keyboard interrupt.')
            break
        # In case of unforeseen errors, the miner will log the error and continue operations.
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            continue

def wait_for_master_process(hotkey, timeout=20):
    start_time = time.time()
    while(True):
        time.sleep(0.1) 
        if hotkey in processes:
            break
        if time.time() - start_time > timeout:
            raise TimeoutError('Timeout while waiting for master process')

# This is the main function, which runs the miner.
if __name__ == "__main__":
    mp.set_start_method('spawn')  # This is often necessary in PyTorch multiprocessing
    main( get_config() )
