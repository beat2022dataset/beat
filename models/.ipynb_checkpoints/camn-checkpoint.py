import torch
import torch.nn as nn
import os
import pickle
import numpy as np
from torch.nn.utils import weight_norm

class Chomp1d(nn.Module):
    def __init__(self, chomp_size):
        super(Chomp1d, self).__init__()
        self.chomp_size = chomp_size

    def forward(self, x):
        return x[:, :, :-self.chomp_size].contiguous()


class TemporalBlock(nn.Module):
    def __init__(self, n_inputs, n_outputs, kernel_size, stride, dilation, padding, dropout=0.2):
        super(TemporalBlock, self).__init__()
        self.conv1 = weight_norm(nn.Conv1d(n_inputs, n_outputs, kernel_size,
                                           stride=stride, padding=padding, dilation=dilation))
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = weight_norm(nn.Conv1d(n_outputs, n_outputs, kernel_size,
                                           stride=stride, padding=padding, dilation=dilation))
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.net = nn.Sequential(self.conv1, self.chomp1, self.relu1, self.dropout1,
                                 self.conv2, self.chomp2, self.relu2, self.dropout2)
        self.downsample = nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        self.relu = nn.ReLU()
        self.init_weights()

    def init_weights(self):
        self.conv1.weight.data.normal_(0, 0.01)
        self.conv2.weight.data.normal_(0, 0.01)
        if self.downsample is not None:
            self.downsample.weight.data.normal_(0, 0.01)

    def forward(self, x):
        out = self.net(x)
        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)


class TemporalConvNet(nn.Module):
    def __init__(self, num_inputs, num_channels, kernel_size=2, dropout=0.2):
        super(TemporalConvNet, self).__init__()
        layers = []
        num_levels = len(num_channels)
        for i in range(num_levels):
            dilation_size = 2 ** i
            in_channels = num_inputs if i == 0 else num_channels[i-1]
            out_channels = num_channels[i]
            layers += [TemporalBlock(in_channels, out_channels, kernel_size, stride=1, dilation=dilation_size,
                                     padding=(kernel_size-1) * dilation_size, dropout=dropout)]

        self.network = nn.Sequential(*layers)

    def forward(self, x):
        return self.network(x)



class TextEncoderTCN(nn.Module):
    """ based on https://github.com/locuslab/TCN/blob/master/TCN/word_cnn/model.py """
    def __init__(self, args, n_words, embed_size=300, pre_trained_embedding=None,
                 kernel_size=2, dropout=0.3, emb_dropout=0.1):
        super(TextEncoderTCN, self).__init__()

        if pre_trained_embedding is not None:  # use pre-trained embedding (fasttext)
            assert pre_trained_embedding.shape[0] == n_words
            assert pre_trained_embedding.shape[1] == embed_size
            self.embedding = nn.Embedding.from_pretrained(torch.FloatTensor(pre_trained_embedding),
                                                          freeze=args.freeze_wordembed)
        else:
            self.embedding = nn.Embedding(n_words, embed_size)

        num_channels = [args.hidden_size] * args.n_layer
        self.tcn = TemporalConvNet(embed_size, num_channels, kernel_size, dropout=dropout)

        self.decoder = nn.Linear(num_channels[-1], args.word_f)
        self.drop = nn.Dropout(emb_dropout)
        self.emb_dropout = emb_dropout
        self.init_weights()

    def init_weights(self):
        self.decoder.bias.data.fill_(0)
        self.decoder.weight.data.normal_(0, 0.01)

    def forward(self, input):
        emb = self.drop(self.embedding(input))
        y = self.tcn(emb.transpose(1, 2)).transpose(1, 2)
        y = self.decoder(y)
        return y.contiguous(), 0



class BasicBlock(nn.Module):
    '''
    from timm
    '''
    def __init__(self, inplanes, planes, ker_size, stride=1, downsample=None, cardinality=1, base_width=64,
                 reduce_first=1, dilation=1, first_dilation=None, act_layer=nn.LeakyReLU,   norm_layer=nn.BatchNorm1d, attn_layer=None, aa_layer=None, drop_block=None, drop_path=None):
        super(BasicBlock, self).__init__()

        self.conv1 = nn.Conv1d(
            inplanes, planes, kernel_size=ker_size, stride=stride, padding=first_dilation,
            dilation=dilation, bias=True)
        self.bn1 = norm_layer(planes)
        self.act1 = act_layer(inplace=True)
        #self.aa = aa_layer(channels=first_planes, stride=stride) if use_aa else None

        self.conv2 = nn.Conv1d(
            planes, planes, kernel_size=ker_size, padding=ker_size//2, dilation=dilation, bias=True)
        self.bn2 = norm_layer(planes)

        #self.se = create_attn(attn_layer, outplanes)

        self.act2 = act_layer(inplace=True)
        if downsample is not None:
            self.downsample = nn.Sequential(
                nn.Conv1d(inplanes, planes,  stride=stride, kernel_size=ker_size, padding=first_dilation, dilation=dilation, bias=True),
                norm_layer(planes), 
            )
        else: self.downsample=None
        self.stride = stride
        self.dilation = dilation
        self.drop_block = drop_block
        self.drop_path = drop_path

    def zero_init_last_bn(self):
        nn.init.zeros_(self.bn2.weight)

    def forward(self, x):
        #print("x after 0", x.shape)
        shortcut = x

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.act1(x)
        #print("x after 1", x.shape)
        x = self.conv2(x)
        x = self.bn2(x)
        #print("x after 2", x.shape)
        if self.downsample is not None:
            shortcut = self.downsample(shortcut)
        x += shortcut
        x = self.act2(x)
        #print("x after 3", x.shape)
        return x



class WavEncoder(nn.Module):
    def __init__(self, out_dim):
        super().__init__() #128*1*140844 
        self.out_dim = out_dim
        self.feat_extractor = nn.Sequential( #b = (a+3200)/5 a 
                BasicBlock(1, 32, 15, 5, first_dilation=1600, downsample=True),
                BasicBlock(32, 32, 15, 6, first_dilation=0, downsample=True),
                BasicBlock(32, 32, 15, 1, first_dilation=7, ),
                BasicBlock(32, 64, 15, 6, first_dilation=0, downsample=True),
                BasicBlock(64, 64, 15, 1, first_dilation=7),
                BasicBlock(64, 128, 15, 6,  first_dilation=0,downsample=True),     
            )
        
    def forward(self, wav_data):
        wav_data = wav_data.unsqueeze(1)  # add channel dim
        out = self.feat_extractor(wav_data)
        return out.transpose(1, 2)  # to (batch x seq x dim)


class PoseGenerator(nn.Module):
    '''
    End2End model
    audio, text and speaker ID encoder are customized based on Yoon et al. SIGGRAPH ASIA 2020
    '''
    def __init__(self, args):
        super().__init__()
        self.pre_length = args.pre_frames #4
        self.gen_length = args.pose_length - args.pre_frames #30
        self.pose_dims = args.pose_dims
        self.facial_f = args.facial_f
        self.speaker_f = args.speaker_f
        self.audio_f = args.audio_f
        self.word_f = args.word_f
        self.emotion_f = args.emotion_f
        self.facial_in = int(args.facial_rep[-2:])
        
        self.in_size = self.audio_f + self.pose_dims + self.facial_f + self.word_f + 1
        self.audio_encoder = WavEncoder(self.audio_f)
        
        self.hidden_size = args.hidden_size
        self.n_layer = args.n_layer

        if self.facial_f is not 0:  
            self.facial_encoder = nn.Sequential( #b = (a+3200)/5 a 
                BasicBlock(self.facial_in, self.facial_f//2, 7, 1, first_dilation=3,  downsample=True),
                BasicBlock(self.facial_f//2, self.facial_f//2, 3, 1, first_dilation=1,  downsample=True),
                BasicBlock(self.facial_f//2, self.facial_f//2, 3, 1, first_dilation=1, ),
                BasicBlock(self.facial_f//2, self.facial_f, 3, 1, first_dilation=1,  downsample=True),   
            )
        else:
            self.facial_encoder = None
            

        self.text_encoder = None   
        if self.word_f is not 0:
            self.text_encoder = TextEncoderTCN(args, 2814+4, 300, pre_trained_embedding=None,
                                           dropout=args.dropout_prob)
            

        self.speaker_embedding = None
        if self.speaker_f is not 0:
            self.in_size += self.speaker_f
#                 self.speaker_embedding_head = nn.Sequential(
#                     nn.Linear(16, self.z_size*2),
#                     nn.Softmax(),)
            self.speaker_embedding =   nn.Sequential(
                nn.Embedding(30, self.speaker_f),# n_words->16
                nn.Linear(self.speaker_f, self.speaker_f), 
                nn.LeakyReLU(True)
            )

            
        self.emotion_embedding = None
        if self.emotion_f is not 0:
            self.in_size += self.emotion_f
            
            self.emotion_embedding =   nn.Sequential(
                nn.Embedding(8, self.emotion_f),# n_words->16
                nn.Linear(self.emotion_f, self.emotion_f) 
            )

            self.emotion_embedding_tail = nn.Sequential( #256*34*8
                nn.Conv1d(self.emotion_f, 8, 9, 1, 4),
                nn.BatchNorm1d(8),
                nn.LeakyReLU(0.3, inplace=True),
                nn.Conv1d(8, 16, 9, 1, 4),
                nn.BatchNorm1d(16),
                nn.LeakyReLU(0.3, inplace=True),
                nn.Conv1d(16, 16, 9, 1, 4),
                nn.BatchNorm1d(16),
                nn.LeakyReLU(0.3, inplace=True),
                nn.Conv1d(16, self.emotion_f, 9, 1, 4),
                nn.BatchNorm1d(self.emotion_f),
                nn.LeakyReLU(0.3, inplace=True),
            )
     

        
        self.gru = nn.GRU(self.in_size, hidden_size=self.hidden_size, num_layers=args.n_layer, batch_first=True,
                          bidirectional=True, dropout=args.dropout_prob)
        self.out = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size//2),
            nn.LeakyReLU(True),
            nn.Linear(self.hidden_size//2, 27)
        )
        
        self.gru_hands = nn.GRU(self.in_size+27, hidden_size=self.hidden_size, num_layers=args.n_layer, batch_first=True,
                          bidirectional=True, dropout=args.dropout_prob)
        self.out_hands = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size//2),
            nn.LeakyReLU(True),
            nn.Linear(self.hidden_size//2, 141-27)
        )

        self.do_flatten_parameters = False
        if torch.cuda.device_count() > 1:
            self.do_flatten_parameters = True
            

    def forward(self, pre_seq, in_audio, in_facial=None, in_text=None, in_id=None, in_emo=None, is_test=False):
        decoder_hidden = decoder_hidden_hands = None
        
        if self.do_flatten_parameters:
            self.gru.flatten_parameters()

        text_feat_seq = audio_feat_seq = None
        if in_audio is not None:
            # audio
            audio_feat_seq = self.audio_encoder(in_audio)  # output (bs, n_frames, feat_size)
        if in_text is not None:
            # text
            text_feat_seq, _ = self.text_encoder(in_text)
            assert(audio_feat_seq.shape[1] == text_feat_seq.shape[1])
        
        if self.facial_f is not 0:
            # facial
            # print(in_facial.shape)
            face_feat_seq = self.facial_encoder(in_facial.permute([0, 2, 1]))
            face_feat_seq = face_feat_seq.permute([0, 2, 1])


        speaker_feat_seq = None
        if self.speaker_embedding: 
            speaker_feat_seq = self.speaker_embedding(in_id)
        emo_feat_seq = None
        if self.emotion_embedding:
            emo_feat_seq = self.emotion_embedding(in_emo)
            emo_feat_seq = emo_feat_seq.permute([0,2,1])
            emo_feat_seq = self.emotion_embedding_tail(emo_feat_seq) # BS*34*8
            emo_feat_seq = emo_feat_seq.permute([0,2,1])

    
        if is_test:
            if self.facial_f is not 0:
                min_length = min(pre_seq.shape[1], audio_feat_seq.shape[1], face_feat_seq.shape[1])
                pre_seq = pre_seq[:,: min_length]
                audio_feat_seq = audio_feat_seq[:, : min_length]
                face_feat_seq = face_feat_seq[:, : min_length]
            else:
                min_length = min(pre_seq.shape[1], audio_feat_seq.shape[1])
                pre_seq = pre_seq[:,: min_length]
                audio_feat_seq = audio_feat_seq[:, : min_length]
            #print(pre_seq.shape)
        if self.audio_f is not 0 and self.facial_f is 0:
            in_data = torch.cat((pre_seq, audio_feat_seq), dim=2)
        elif self.audio_f is not 0 and self.facial_f is not 0:
            in_data = torch.cat((pre_seq, audio_feat_seq, face_feat_seq), dim=2)
        else: pass
        
        if text_feat_seq is not None:
            in_data = torch.cat((in_data, text_feat_seq), dim=2)
        if emo_feat_seq is not None:
            in_data = torch.cat((in_data, emo_feat_seq), dim=2)
        
        if speaker_feat_seq is not None:
            #if print(z_context.shape)
            repeated_s = speaker_feat_seq
            #print(repeated_s.shape)
            if len(repeated_s.shape) == 2:
                repeated_s = repeated_s.reshape(1, repeated_s.shape[1], repeated_s.shape[0])
                #print(repeated_s.shape)
            repeated_s = repeated_s.repeat(1, in_data.shape[1], 1)
            #print(repeated_s.shape)
            #print(repeated_s.shape)
            in_data = torch.cat((in_data, repeated_s), dim=2)
        
        
        output, decoder_hidden = self.gru(in_data, decoder_hidden)
        output = output[:, :, :self.hidden_size] + output[:, :, self.hidden_size:]  # sum bidirectional outputs
        output = self.out(output.reshape(-1, output.shape[2]))
        decoder_outputs = output.reshape(in_data.shape[0], in_data.shape[1], -1)
        
        in_data = torch.cat((in_data, decoder_outputs), dim=2)
        output_hands, decoder_hidden_hands = self.gru_hands(in_data, decoder_hidden_hands)
        output_hands = output_hands[:, :, :self.hidden_size] + output_hands[:, :, self.hidden_size:]
        output_hands = self.out_hands(output_hands.reshape(-1, output_hands.shape[2]))
        decoder_outputs_hands = output_hands.reshape(in_data.shape[0], in_data.shape[1], -1)
        
        decoder_outputs_final = torch.zeros((in_data.shape[0], in_data.shape[1], 141)).cuda()
        decoder_outputs_final[:, :, 0:18] = decoder_outputs[:, :, 0:18]
        decoder_outputs_final[:, :, 18:75] = decoder_outputs_hands[:, :, 0:57]
        decoder_outputs_final[:, :, 75:84] = decoder_outputs[:, :, 18:27]
        decoder_outputs_final[:, :, 84:141] = decoder_outputs_hands[:, :, 57:114]


        return decoder_outputs_final
    

class CaMN(PoseGenerator):
    def __init__(self, args):
        super().__init__(args) 
        self.audio_fusion_dim = self.audio_f+self.speaker_f+self.emotion_f+self.word_f
        self.facial_fusion_dim = self.audio_fusion_dim + self.facial_f
        self.audio_fusion = nn.Sequential(
            nn.Linear(self.audio_fusion_dim, self.hidden_size//2),
            nn.LeakyReLU(True),
            nn.Linear(self.hidden_size//2, self.audio_f),
            nn.LeakyReLU(True),
        )
        
        self.facial_fusion = nn.Sequential(
            nn.Linear(self.facial_fusion_dim, self.hidden_size//2),
            nn.LeakyReLU(True),
            nn.Linear(self.hidden_size//2, self.facial_f),
            nn.LeakyReLU(True),
        )
        
        
    def forward(self, pre_seq, in_audio=None, in_facial=None, in_text=None, in_id=None, in_emo=None):
        if self.do_flatten_parameters:
            self.gru.flatten_parameters()
            
        decoder_hidden = decoder_hidden_hands = None
        text_feat_seq = audio_feat_seq = speaker_feat_seq = emo_feat_seq = face_feat_seq =  None
        in_data = None
        
        if self.speaker_embedding: 
            speaker_feat_seq = self.speaker_embedding(in_id)
        
            if len(speaker_feat_seq.shape) == 2:
                speaker_feat_seq = speaker_feat_seq.reshape(1, speaker_feat_seq.shape[1], speaker_feat_seq.shape[0])
                #print(repeated_s.shape)
            speaker_feat_seq = speaker_feat_seq.repeat(1, pre_seq.shape[1], 1)
            
            in_data = torch.cat((in_data, speaker_feat_seq), 2) if in_data is not None else speaker_feat_seq

        if self.emotion_embedding:
            emo_feat_seq = self.emotion_embedding(in_emo)
            emo_feat_seq = emo_feat_seq.permute([0,2,1])
            emo_feat_seq = self.emotion_embedding_tail(emo_feat_seq) # BS*34*8
            emo_feat_seq = emo_feat_seq.permute([0,2,1])
            #print(in_data.shape, emo_feat_seq.shape)
            in_data = torch.cat((in_data, emo_feat_seq), 2) if in_data is not None else emo_feat_seq
            
        if in_text is not None:
            text_feat_seq, _ = self.text_encoder(in_text)
            in_data = torch.cat((in_data, text_feat_seq), 2) if in_data is not None else text_feat_seq
            
        if in_audio is not None:
            # audio
            audio_feat_seq = self.audio_encoder(in_audio) #bs*34*128
            if (audio_feat_seq.shape[1] != text_feat_seq.shape[1]):
                audio_feat_seq = torch.cat((audio_feat_seq, audio_feat_seq[:,-1, :].reshape(1,1,-1)),1)
                #print(audio_feat_seq.shape, text_feat_seq.shape, pre_seq.shape)
                
            audio_fusion_seq = self.audio_fusion(torch.cat((audio_feat_seq, emo_feat_seq, speaker_feat_seq, text_feat_seq), dim=2).reshape(-1, self.audio_fusion_dim))
            audio_feat_seq = audio_fusion_seq.reshape(*audio_feat_seq.shape)
            in_data = torch.cat((in_data, audio_feat_seq), 2) if in_data is not None else audio_feat_seq
        
        if self.facial_f is not 0:
            face_feat_seq = self.facial_encoder(in_facial.permute([0, 2, 1]))
            face_feat_seq = face_feat_seq.permute([0, 2, 1])
        
            face_fusion_seq = self.facial_fusion(torch.cat((face_feat_seq, audio_feat_seq, emo_feat_seq, speaker_feat_seq, text_feat_seq), dim=2).reshape(-1, self.facial_fusion_dim))
            face_feat_seq = face_fusion_seq.reshape(*face_feat_seq.shape)
            in_data = torch.cat((in_data, face_feat_seq), 2) if in_data is not None else face_feat_seq
            
            
        in_data = torch.cat((pre_seq, in_data), dim=2)
        output, decoder_hidden = self.gru(in_data, decoder_hidden)
        output = output[:, :, :self.hidden_size] + output[:, :, self.hidden_size:]  # sum bidirectional outputs
        output = self.out(output.reshape(-1, output.shape[2]))
        decoder_outputs = output.reshape(in_data.shape[0], in_data.shape[1], -1)
        
        in_data = torch.cat((in_data, decoder_outputs), dim=2)
        output_hands, decoder_hidden_hands = self.gru_hands(in_data, decoder_hidden_hands)
        output_hands = output_hands[:, :, :self.hidden_size] + output_hands[:, :, self.hidden_size:]
        output_hands = self.out_hands(output_hands.reshape(-1, output_hands.shape[2]))
        decoder_outputs_hands = output_hands.reshape(in_data.shape[0], in_data.shape[1], -1)
        
        decoder_outputs_final = torch.zeros((in_data.shape[0], in_data.shape[1], 141)).cuda()
        decoder_outputs_final[:, :, 0:18] = decoder_outputs[:, :, 0:18]
        decoder_outputs_final[:, :, 18:75] = decoder_outputs_hands[:, :, 0:57]
        decoder_outputs_final[:, :, 75:84] = decoder_outputs[:, :, 18:27]
        decoder_outputs_final[:, :, 84:141] = decoder_outputs_hands[:, :, 57:114]
        
        return decoder_outputs_final

    
class ConvDiscriminator(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.input_size = args.pose_dims

        self.hidden_size = 64
        self.pre_conv = nn.Sequential(
            nn.Conv1d(self.input_size, 16, 3),#34-32
            nn.BatchNorm1d(16),
            nn.LeakyReLU(True),
            nn.Conv1d(16, 8, 3),#30
            nn.BatchNorm1d(8),
            nn.LeakyReLU(True),
            nn.Conv1d(8, 8, 3),
        )

        self.gru = nn.GRU(8, hidden_size=self.hidden_size, num_layers=4, bidirectional=True,
                          dropout=0.3, batch_first=True)
        self.out = nn.Linear(self.hidden_size, 1)
        self.out2 = nn.Linear(34-6, 1) # 28, 62
       
        self.do_flatten_parameters = False
        if torch.cuda.device_count() > 1:
            self.do_flatten_parameters = True

    def forward(self, poses):
        decoder_hidden = None
        if self.do_flatten_parameters:
            self.gru.flatten_parameters()
        #print('step0:', poses.shape)
        poses = poses.transpose(1, 2)
        #print('step1:', poses.shape)
        feat = self.pre_conv(poses)
        #print('step2:', feat.shape)
        feat = feat.transpose(1, 2)
        #print('step3:', feat.shape)
         
        output, decoder_hidden = self.gru(feat, decoder_hidden)
        #print('step4:', output.shape)
        output = output[:, :, :self.hidden_size] + output[:, :, self.hidden_size:]  # sum bidirectional outputs
        #print('step5:', output.shape)

        # use the last N outputs
        batch_size = poses.shape[0]
        output = output.contiguous().view(-1, output.shape[2])
        #print('step6:', output.shape)
        output = self.out(output)  # apply linear to every output
        #print('step7:', output.shape)
        output = output.view(batch_size, -1)
        output = self.out2(output)
        #print('step8:', output.shape)
        output = torch.sigmoid(output)

        return output
