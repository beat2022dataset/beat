import os
import pickle
import math
import shutil
import numpy as np
import lmdb as lmdb
import textgrid as tg
import pandas as pd
import torch
import glob
import json
from termcolor import colored
from loguru import logger
from collections import defaultdict
from torch.utils.data import Dataset
import pyarrow
from sklearn.preprocessing import normalize
# import librosa 
import scipy.io.wavfile
from scipy import signal
from .build_vocab import Vocab


class CustomDataset(Dataset):
    def __init__(self, args, loader_type, augmentation=None, kwargs=None, build_cache=True):
        self.loader_type = loader_type
        self.new_cache = args.new_cache
        self.pose_length = args.pose_length #34
        self.stride = args.stride #10
        self.pose_fps = args.pose_fps #15
        self.pose_dims = args.pose_dims # 141
        self.mean_pose = np.load(args.root_path+args.mean_pose_path+f"{args.pose_rep}/mean.npy")
        self.std_pose = np.load(args.root_path+args.std_pose_path+f"{args.pose_rep}/std.npy")
        self.audio_norm = args.audio_norm
        if self.audio_norm:
            self.mean_audio = np.load(args.mean_audio_path+f"{args.audio_rep}/mean.npy")
            self.std_audio = np.load(args.std_audio_path+f"{args.audio_rep}/std.npy")
     
        self.loader_type = loader_type
        self.audio_rep = args.audio_rep
        self.pose_rep = args.pose_rep
        self.facial_rep = args.facial_rep
        self.word_rep = args.word_rep
        self.emo_rep = args.emo_rep
        self.sem_rep = args.sem_rep
        self.audio_fps = args.audio_fps
        self.speaker_id = args.speaker_id
        
        self.disable_filtering = args.disable_filtering
        self.clean_first_seconds = args.clean_first_seconds
        self.clean_final_seconds = args.clean_final_seconds
        
        if loader_type == "train":
            self.data_dir = args.root_path + args.train_data_path
        elif loader_type == "val":
            self.data_dir = args.root_path + args.val_data_path
        else:
            self.data_dir = args.root_path + args.test_data_path
        
        if self.word_rep is not "None":
            with open("../../datasets/beat/vocab.pkl", 'rb') as f:
                self.lang_model = pickle.load(f)
        if build_cache:
            self.build_cache()
            
        
     
            
    def build_cache(self):
        logger.info(f"Audio bit rate: {self.audio_fps}")
        logger.info("Reading data '{}'...".format(self.data_dir))
        preloaded_dir = self.data_dir + f"{self.pose_rep}_cache"
        # pose_length_extended = int(round(self.pose_length))
        logger.info("Creating the dataset cache...")

        if self.new_cache:
            if os.path.exists(preloaded_dir):
                shutil.rmtree(preloaded_dir)

        if os.path.exists(preloaded_dir):
            logger.info("Found the cache {}".format(preloaded_dir))
        elif self.loader_type == "test":
            self.cache_generation(
                preloaded_dir, True, 
                0, 0,
                is_test=True)
        else: 
            self.cache_generation(
                preloaded_dir, self.disable_filtering, 
                self.clean_first_seconds, self.clean_final_seconds,
                is_test=False)

        self.lmdb_env = lmdb.open(preloaded_dir, readonly=True, lock=False)
        with self.lmdb_env.begin() as txn:
            self.n_samples = txn.stat()["entries"]
        
    
    def __len__(self):
        #print("in_dataset:", self.n_samples)
        return self.n_samples

    def cache_generation(self, out_lmdb_dir, disable_filtering, clean_first_seconds,  clean_final_seconds, is_test=False):
        self.n_out_samples = 0
        audio_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.audio_rep}",) + "/*.wav"), key=str,)
        pose_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.pose_rep}") + "/*.bvh"), key=str,)
        # print(audio_files, pose_files)
        if self.facial_rep != "None":
            facial_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.facial_rep}") + "/*.json"),key=str,)
        else: 
            facial_files = []
            
        if self.word_rep != "None":
            word_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.word_rep}") + "/*.TextGrid"),key=str,)
        else: 
            word_files = []
            
        if self.emo_rep != "None":
            emo_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.emo_rep}") + "/*.txt"),key=str,)
        else: 
            emo_files = []
            
        if self.sem_rep != "None":
            sem_files = sorted(glob.glob(os.path.join(self.data_dir, f"{self.sem_rep}") + "/*.txt"),key=str,)
        else: 
            sem_files = []
            
        # create db for samples
        map_size = int(1024 * 1024 * 2048 * (self.audio_fps/16000)**3 * 4)  # in 1024 MB
        dst_lmdb_env = lmdb.open(out_lmdb_dir, map_size=map_size)
        n_filtered_out = defaultdict(int)
        counter_audio = -1
        for audio_file in audio_files:
            audio_each_file = []
            pose_each_file = [] 
            facial_each_file = []
            word_each_file = []
            vid_each_file = []
            emo_each_file = []
            sem_each_file = []
            
            id_audio = audio_file[-11:-4] #000_000 
            exist = False
            for pose_file in range(len(pose_files)):
                id_pose = pose_files[pose_file][-11:-4]
                if id_audio == id_pose:
                    logger.info(colored(f"# ---- Building cache for Audio {id_audio} and Pose {id_pose} ---- #", "blue"))
                    id_pose = pose_file
                    exist = True
                    counter_audio += 1
                    break
            if not exist: continue
            
            if self.speaker_id:
                vid_each_file.append(int(id_audio[:3]))
            
            # the librosa cannot use on the cloud sever
            # audio_data, _ = librosa.load(audio_file, sr=None)
            if self.audio_rep == "melspec":
                audio_each_file = np.load(f"{audio_file[:-4]}_melspec_128_64.npy").transpose(1,0)
                self.audio_fps = 32
            elif self.audio_rep == "disentangled":
                audio_each_file = np.load(f"{audio_file[:-4]}_disentangled_v1.npy").transpose(1,0)
            else:
                sr, audio_each_file = scipy.io.wavfile.read(audio_file) # np array
                audio_each_file = audio_each_file[::sr//16000]
                
            if self.audio_norm:
                audio_each_file = (audio_each_file - self.mean_audio) / self.std_audio
                
            with open(pose_files[id_pose], "r") as pose_data:
                for j, line in enumerate(pose_data.readlines()):
                    data = np.fromstring(line, dtype=float, sep=" ") # 1*27 e.g., 27 rotation 
                    pose_each_file.append(data)
            pose_each_file = np.array(pose_each_file) # n frames * 27
        
            
            if len(facial_files) != 0:
                for facial_file in range(len(facial_files)):
                    id_facial = facial_files[facial_file][-12:-5]
                    if id_audio == id_facial:
                        logger.info(f"# ---- Building cache for Audio {id_audio} and Facial {id_facial} ---- #")
                        id_facial = facial_file
                        break
                with open(facial_files[id_facial], 'r') as facial_data_file:
                    facial_data = json.load(facial_data_file)
                    facial_factor = math.ceil(1/((facial_data['frames'][20]['time'] - facial_data['frames'][10]['time'])/10))//self.pose_fps
                    #print(facial_data['frames'][20]['time'] - facial_data['frames'][10]['time']) 
                    for j, frame_data in enumerate(facial_data['frames']):
                        # 60FPS to 15FPS
                        if j % facial_factor == 0:
                            facial_each_file.append(frame_data['weights']) 
                facial_each_file = np.array(facial_each_file)
                
            if len(word_files) != 0:
                for word_file in range(len(word_files)):
                    id_word = word_files[word_file][-16:-9]
                    if id_audio == id_word:
                        logger.info(f"# ---- Building cache for Audio {id_audio} and Word {id_word} ---- #")
                        id_word = word_file
                        break
                tgrid = tg.TextGrid.fromFile(word_files[id_word])
                for i in range(pose_each_file.shape[0]):
                    found_flag = False
                    current_time = i/self.pose_fps
                    for word in tgrid[0]:
                        word_n, word_s, word_e = word.mark, word.minTime, word.maxTime
                        if word_s<=current_time and current_time<=word_e:
                            if word_n == " ":
                                #TODO now don't have eos and sos token
                                word_each_file.append(self.lang_model.PAD_token)
                            else:    
                                word_each_file.append(self.lang_model.get_word_index(word_n))
                            found_flag = True
                            break
                        else: continue   
                    if not found_flag: word_each_file.append(self.lang_model.UNK_token)
                word_each_file = np.array(word_each_file)
                #print(word_each_file)    
                    
            
            if len(emo_files) != 0:
                for emo_file in range(len(emo_files)):
                    id_emo = emo_files[emo_file][-11:-4]
                    if id_audio == id_emo:
                        logger.info(f"# ---- Building cache for Audio {id_audio} and Emo {id_emo} ---- #")
                        id_emo = emo_file
                        break
                emo_all = pd.read_csv(emo_files[id_emo], 
                    sep='\t', 
                    names=["name", "x", "start_time", "start", "end_time", "end", "duration_time", "duration", "score"])
                for i in range(pose_each_file.shape[0]):
                    found_flag = False
                    for j, (start, end, score) in enumerate(zip(emo_all['start'],emo_all['end'], emo_all['score'])):
                        current_time = i/self.pose_fps
                        if start<=current_time and current_time<=end: 
                            emo_each_file.append(score)
                            found_flag=True
                            break
                        else: continue 
                    if not found_flag: emo_each_file.append(0)
                emo_each_file = np.array(emo_each_file)
                #print(emo_each_file)
                
            if len(sem_files) != 0:
                for sem_file in range(len(sem_files)):
                    id_sem = sem_files[sem_file][-11:-4]
                    if id_audio == id_sem:
                        logger.info(f"# ---- Building cache for Audio {id_audio} and Sem {id_sem} ---- #")
                        id_sem = sem_file
                        break
                sem_all = pd.read_csv(sem_files[id_sem], 
                    sep='\t', 
                    names=["name", "x", "start_time", "start", "end_time", "end", "duration_time", "duration", "score"])
                for i in range(pose_each_file.shape[0]):
                    found_flag = False
                    for j, (start, end, score) in enumerate(zip(sem_all['start'],sem_all['end'], sem_all['score'])):
                        current_time = i/self.pose_fps
                        if start<=current_time and current_time<=end: 
                            sem_each_file.append(score)
                            found_flag=True
                            break
                        else: continue 
                    if not found_flag: sem_each_file.append(0.)
                sem_each_file = np.array(sem_each_file)
                #print(sem_each_file)
                
            filtered_result = self._sample_from_clip(
                dst_lmdb_env,
                audio_each_file, pose_each_file, facial_each_file, word_each_file,
                vid_each_file, emo_each_file, sem_each_file,
                disable_filtering, clean_first_seconds, clean_final_seconds, is_test,
                ) 
            for type in filtered_result.keys():
                n_filtered_out[type] += filtered_result[type]
                                
            
        # print stats
        with dst_lmdb_env.begin() as txn:
            logger.info(colored(f"no. of samples: {txn.stat()['entries']}", "cyan"))
            n_total_filtered = 0
            for type, n_filtered in n_filtered_out.items():
                logger.info("{}: {}".format(type, n_filtered))
                n_total_filtered += n_filtered
            logger.info(colored("no. of excluded samples: {} ({:.1f}%)".format(
                n_total_filtered, 100 * n_total_filtered / (txn.stat()["entries"] + n_total_filtered)), "cyan"))
        dst_lmdb_env.sync()
        dst_lmdb_env.close()
    
    def _sample_from_clip(
        self, dst_lmdb_env, audio_each_file, pose_each_file, facial_each_file, word_each_file,
        vid_each_file, emo_each_file, sem_each_file,
        disable_filtering, clean_first_seconds, clean_final_seconds, is_test,
        ):
        """
        for data cleaning, we ignore the data for first and final n s
        for test, we return all data 
        """
        #logger.info(f"alignment: {alignment}")
        audio_start = 0 #int(alignment[0] * self.audio_fps)
        pose_start = 0 #int(alignment[1] * self.pose_fps)
#         print(audio_each_file)
#         print(pose_each_file)
        logger.info(f"before: {audio_each_file.shape} {pose_each_file.shape}")
        audio_each_file = audio_each_file[pose_start:]
        pose_each_file = pose_each_file[pose_start:]
        logger.info(f"after: {audio_each_file.shape} {pose_each_file.shape}")
        round_seconds_skeleton = pose_each_file.shape[0] // self.pose_fps  # assume 1500 frames / 15 fps = 100 s
        round_seconds_audio = len(audio_each_file) // self.audio_fps # assume 16,000,00 / 16,000 = 100 s
        if facial_each_file != []:
            round_seconds_facial = facial_each_file.shape[0] // self.pose_fps
            logger.info(f"audio: {round_seconds_skeleton}s, pose: {round_seconds_audio}s, facial: {round_seconds_facial}s")
            round_seconds_skeleton = min(round_seconds_audio, round_seconds_skeleton, round_seconds_facial)
            max_round = max(round_seconds_audio, round_seconds_skeleton, round_seconds_facial)
            if round_seconds_skeleton != max_round: 
                logger.warning(f"reduce to {round_seconds_skeleton}s, ignore {max_round-round_seconds_skeleton}s")  
        else:
            logger.info(f"audio: {round_seconds_skeleton}s, pose: {round_seconds_audio}s")
            round_seconds_skeleton = min(round_seconds_audio, round_seconds_skeleton)
            max_round = max(round_seconds_audio, round_seconds_skeleton)
            if round_seconds_skeleton != max_round: 
                logger.warning(f"reduce to {round_seconds_skeleton}s, ignore {max_round-round_seconds_skeleton}s")
        
        clip_s_t, clip_e_t = clean_first_seconds, round_seconds_skeleton - clean_final_seconds # assume [10, 90]s
        clip_s_f_audio, clip_e_f_audio = self.audio_fps * clip_s_t, clip_e_t * self.audio_fps # [160,000,90*160,000]
        clip_s_f_pose, clip_e_f_pose = clip_s_t * self.pose_fps, clip_e_t * self.pose_fps # [150,90*15]

        if is_test:# stride = length for test
            self.pose_length = clip_e_f_pose - clip_s_f_pose
            self.stride = self.pose_length
        audio_short_length = math.floor(self.pose_length / self.pose_fps * self.audio_fps)
        num_subdivision = math.floor((clip_e_f_pose - clip_s_f_pose - self.pose_length) / self.stride) + 1
        """
        for audio sr = 16000, fps = 15, pose_length = 34, 
        audio short length = 36266.7 -> 36266
        this error is fine.
        """
        logger.info(f"audio from frame {clip_s_f_audio} to {clip_e_f_audio}, length {audio_short_length}")
        logger.info(f"pose from frame {clip_s_f_pose} to {clip_e_f_pose}, length {self.pose_length}")
        logger.info(f"{num_subdivision} clips is expected with stride {self.stride}") 

        n_filtered_out = defaultdict(int)
        sample_pose_list = []
        sample_audio_list = []
        sample_facial_list = []
        sample_word_list = []
        sample_vid_list = []
        sample_emo_list = []
        sample_sem_list = []
        
        for i in range(num_subdivision): # cut into around 2s chip, (self npose)
            start_idx = clip_s_f_pose + i * self.stride
            fin_idx = start_idx + self.pose_length # 34
            audio_start = clip_s_f_audio + math.floor(i * self.stride * self.audio_fps / self.pose_fps)
            audio_end = audio_start + audio_short_length
            # print(start_idx, fin_idx, audio_start, audio_end)
            sample_pose = pose_each_file[start_idx:fin_idx]
            # print(sample_pose.shape)
 
            if audio_end > clip_e_f_audio:  # correct size mismatch between poses and audio
                n_padding = audio_end - clip_e_f_audio
                logger.warning(f"padding audio for length {n_padding}")
                padded_data = np.pad(audio_each_file, (0, n_padding), mode="symmetric")
                sample_audio = padded_data[audio_start:audio_end]
            else:
                sample_audio = audio_each_file[audio_start:audio_end]

            
            if facial_each_file != []: 
                sample_facial = facial_each_file[start_idx:fin_idx]
                # print(sample_facial.shape)
                # print(sample_pose.shape)
                if sample_pose.shape[0] != sample_facial.shape[0]:
                    logger.warning(f"skip {sample_pose.shape}, {sample_facial.shape}")
                    continue
            else: 
                sample_facial = np.array([-1])
                
            sample_vid = np.array(vid_each_file) if vid_each_file != [] else np.array([-1])
            
            start_time = start_idx/self.pose_fps
            end_time = fin_idx/self.pose_fps
            sample_word = []
            if word_each_file != []:
                sample_word = word_each_file[start_idx:fin_idx]
            else: sample_word = np.array([-1])    
            #print(sample_word)
            
            if emo_each_file != []:
                sample_emo = emo_each_file[start_idx:fin_idx]
                #print(sample_emo)
            else:                   
                sample_emo = np.array([-1])                      
 
            if sem_each_file != []:
                sample_sem = sem_each_file[start_idx:fin_idx]
                #print(sample_sem)
            else:   
                sample_sem = np.array([-1])
            
                                  
            if sample_audio.any() != None:
                # filtering motion skeleton data
                sample_pose, filtering_message = MotionPreprocessor(sample_pose, self.mean_pose).get()
                is_correct_motion = (sample_pose != [])
                if is_correct_motion or disable_filtering:
                    sample_pose_list.append(sample_pose)
                    sample_audio_list.append(sample_audio)
                    sample_facial_list.append(sample_facial)
                    sample_word_list.append(sample_word)
                    sample_vid_list.append(sample_vid)
                    sample_emo_list.append(sample_emo)
                    sample_sem_list.append(sample_sem)
                else:
                    n_filtered_out[filtering_message] += 1
                
        if len(sample_pose_list) > 0:
            with dst_lmdb_env.begin(write=True) as txn:
               
                for pose, audio, facial, word, vid, emo, sem in zip(sample_pose_list,
                                                    sample_audio_list,
                                                    sample_facial_list,
                                                    sample_word_list,
                                                    sample_vid_list,
                                                    sample_emo_list,
                                                    sample_sem_list, 
                                                    ):
                    normalized_pose = self.normalize_pose(pose, self.mean_pose, self.std_pose)
                      
                    # save
                    k = "{:005}".format(self.n_out_samples).encode("ascii")
                    v = [normalized_pose, audio, facial, word, vid, emo, sem]
                    # print(v)
                    v = pyarrow.serialize(v).to_buffer()
                    txn.put(k, v)
                    self.n_out_samples += 1
        return n_filtered_out


    @staticmethod
    def normalize_pose(dir_vec, mean_pose, std_pose=None):
        return (dir_vec - mean_pose) / std_pose 

    @staticmethod
    def unnormalize_data(normalized_data, data_mean, data_std, dimensions_to_ignore):
        """
        this method is from https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/CRFProblems/H3.6m/generateMotionData.py#L12
        """
        T = normalized_data.shape[0]
        D = data_mean.shape[0]

        origData = np.zeros((T, D), dtype=np.float32)
        dimensions_to_use = []
        for i in range(D):
            if i in dimensions_to_ignore:
                continue
            dimensions_to_use.append(i)
        dimensions_to_use = np.array(dimensions_to_use)

        origData[:, dimensions_to_use] = normalized_data

        # potentially inefficient, but only done once per experiment
        stdMat = data_std.reshape((1, D))
        stdMat = np.repeat(stdMat, T, axis=0)
        meanMat = data_mean.reshape((1, D))
        meanMat = np.repeat(meanMat, T, axis=0)
        origData = np.multiply(origData, stdMat) + meanMat

        return origData
   
    
    def __getitem__(self, idx):
        with self.lmdb_env.begin(write=False) as txn:
            key = "{:005}".format(idx).encode("ascii")
            sample = txn.get(key)
            sample = pyarrow.deserialize(sample)
            
            tar_pose, in_audio, in_facial, in_word, vid, emo, sem = sample
            vid = torch.from_numpy(vid).int()
            
            emo = torch.from_numpy(emo).int()
            sem = torch.from_numpy(sem).float() 
            in_audio = torch.from_numpy(in_audio).float() 
            in_word = torch.from_numpy(in_word).int()  
        
            if self.loader_type == "test":
                tar_pose = torch.from_numpy(tar_pose).float()
                in_facial = torch.from_numpy(in_facial).float()
                            
            else:
                tar_pose = torch.from_numpy(tar_pose).reshape((tar_pose.shape[0], -1)).float()
                in_facial = torch.from_numpy(in_facial).reshape((in_facial.shape[0], -1)).float()
            return tar_pose, in_audio, in_facial, in_word, vid, emo, sem

         
class MotionPreprocessor:
    def __init__(self, skeletons, mean_pose):
        self.skeletons = skeletons
        self.mean_pose = mean_pose
        self.filtering_message = "PASS"

    def get(self):
        assert (self.skeletons is not None)

        # filtering
        if self.skeletons != []:
            if self.check_pose_diff():
                self.skeletons = []
                self.filtering_message = "pose"
            # elif self.check_spine_angle():
            #     self.skeletons = []
            #     self.filtering_message = "spine angle"
            # elif self.check_static_motion():
            #     self.skeletons = []
            #     self.filtering_message = "motion"

        # if self.skeletons != []:
        #     self.skeletons = self.skeletons.tolist()
        #     for i, frame in enumerate(self.skeletons):
        #         assert not np.isnan(self.skeletons[i]).any()  # missing joints

        return self.skeletons, self.filtering_message

    def check_static_motion(self, verbose=True):
        def get_variance(skeleton, joint_idx):
            wrist_pos = skeleton[:, joint_idx]
            variance = np.sum(np.var(wrist_pos, axis=0))
            return variance

        left_arm_var = get_variance(self.skeletons, 6)
        right_arm_var = get_variance(self.skeletons, 9)

        th = 0.0014  # exclude 13110
        # th = 0.002  # exclude 16905
        if left_arm_var < th and right_arm_var < th:
            if verbose:
                print("skip - check_static_motion left var {}, right var {}".format(left_arm_var, right_arm_var))
            return True
        else:
            if verbose:
                print("pass - check_static_motion left var {}, right var {}".format(left_arm_var, right_arm_var))
            return False


    def check_pose_diff(self, verbose=False):
        diff = np.abs(self.skeletons - self.mean_pose) # 186*1
        diff = np.mean(diff)

        # th = 0.017
        th = 0.02 #0.02  # exclude 3594
        if diff < th:
            if verbose:
                print("skip - check_pose_diff {:.5f}".format(diff))
            return True
#         th = 3.5 #0.02  # exclude 3594
#         if 3.5 < diff < 5:
#             if verbose:
#                 print("skip - check_pose_diff {:.5f}".format(diff))
#             return True
        else:
            if verbose:
                print("pass - check_pose_diff {:.5f}".format(diff))
            return False


    def check_spine_angle(self, verbose=True):
        def angle_between(v1, v2):
            v1_u = v1 / np.linalg.norm(v1)
            v2_u = v2 / np.linalg.norm(v2)
            return np.arccos(np.clip(np.dot(v1_u, v2_u), -1.0, 1.0))

        angles = []
        for i in range(self.skeletons.shape[0]):
            spine_vec = self.skeletons[i, 1] - self.skeletons[i, 0]
            angle = angle_between(spine_vec, [0, -1, 0])
            angles.append(angle)

        if np.rad2deg(max(angles)) > 30 or np.rad2deg(np.mean(angles)) > 20:  # exclude 4495
        # if np.rad2deg(max(angles)) > 20:  # exclude 8270
            if verbose:
                print("skip - check_spine_angle {:.5f}, {:.5f}".format(max(angles), np.mean(angles)))
            return True
        else:
            if verbose:
                print("pass - check_spine_angle {:.5f}".format(max(angles)))
            return False
