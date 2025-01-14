import torch
import torch.nn as nn
import timm
import math
from timm.models.vision_transformer import Block
from models.swin import SwinTransformer
# from swin import SwinTransformer
from torch import nn
from einops import rearrange
from models.TokenSelect import TokenSelect 

from torch.fft import fft2, ifft2, fftshift, ifftshift
#跨通道注意力
class BasicConv(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, groups=1, relu=True, bn=True, bias=False):
        super(BasicConv, self).__init__()
        self.out_channels = out_planes
        # self.conv = FastFourierConv(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, groups=groups, bias=bias)
        
        self.conv = nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, groups=groups, bias=bias)
        self.bn = nn.BatchNorm2d(out_planes,eps=1e-5, momentum=0.01, affine=True) if bn else None
        self.relu = nn.ReLU() if relu else None

    def forward(self, x):
        x = self.conv(x)
        if self.bn is not None:

            #此处异常
            x = self.bn(x)
        if self.relu is not None:
            x = self.relu(x)
        return x

class ZPool(nn.Module):
    def forward(self, x):
        # 以建立CW之间的交互为例, x:(B, H, C, W)
        a = torch.max(x,1)[0].unsqueeze(1) # 全局最大池化: (B, H, C, W)->(B, 1, C, W);  torch.max返回的是数组:[最大值,对应索引]
        b = torch.mean(x,1).unsqueeze(1)   # 全局平均池化: (B, H, C, W)->(B, 1, C, W);
        c = torch.cat((a, b), dim=1)       # 在对应维度拼接最大和平均特征: (B, 2, C, W)
        return c

class AttentionGate(nn.Module):
    def __init__(self):
        super(AttentionGate, self).__init__()
        kernel_size = 7
        self.compress = ZPool()
        self.conv = BasicConv(2, 1, kernel_size, stride=1, padding=(kernel_size-1) // 2, relu=False)
    def forward(self, x):
        # 以建立CW之间的交互为例, x:(B, H, C, W)
        x_compress = self.compress(x) # 在对应维度上执行最大池化和平均池化,并将其拼接: (B, H, C, W) --> (B, 2, C, W);
        x_out = self.conv(x_compress) # 通过conv操作将最大池化和平均池化特征映射到一维: (B, 2, C, W) --> (B, 1, C, W);
        scale = torch.sigmoid_(x_out) # 通过sigmoid函数生成权重: (B, 1, C, W);
        return x * scale              # 对输入进行重新加权表示: (B, H, C, W) * (B, 1, C, W) = (B, H, C, W)

class MultiDimensional (nn.Module):
    def __init__(self, no_spatial=False):
        super(MultiDimensional , self).__init__()
        self.cw = AttentionGate()
        self.hc = AttentionGate()
        self.no_spatial=no_spatial
        if not no_spatial:
            self.hw = AttentionGate()
    def forward(self, x):
        # 建立C和W之间的交互:
        x = rearrange(x,'b c (h w)-> b c h w',h = 28,w = 28)
        x_perm1 = x.permute(0,2,1,3).contiguous() # (B, C, H, W)--> (B, H, C, W);  执行“旋转操作”,建立C和W之间的交互,所以要在H维度上压缩
        x_out1 = self.cw(x_perm1) # (B, H, C, W)-->(B, H, C, W);  在H维度上进行压缩、拼接、Conv、sigmoid操作, 然后通过权重重新加权
        x_out11 = x_out1.permute(0,2,1,3).contiguous() # 恢复与输入相同的shape,也就是重新旋转回来: (B, H, C, W)-->(B, C, H, W)

        # 建立H和C之间的交互:
        x_perm2 = x.permute(0,3,2,1).contiguous() # (B, C, H, W)--> (B, W, H, C); 执行“旋转操作”,建立H和C之间的交互,所以要在W维度上压缩
        x_out2 = self.hc(x_perm2) # (B, W, H, C)-->(B, W, H, C);  在W维度上进行压缩、拼接、Conv、sigmoid操作, 然后通过权重重新加权
        x_out21 = x_out2.permute(0,3,2,1).contiguous() # 恢复与输入相同的shape,也就是重新旋转回来: (B, W, H, C)-->(B, C, H, W)

        # 建立H和W之间的交互:
        if not self.no_spatial:
            x_out = self.hw(x) # (B, C, H, W)-->(B, C, H, W);  在C维度上进行压缩、拼接、Conv、sigmoid操作, 然后通过权重重新加权
            x_out = 1/3 * (x_out + x_out11 + x_out21) # 取三部分的平均值进行输出
        else:
            x_out = 1/2 * (x_out11 + x_out21)
        x_out = rearrange(x_out,'b c h w-> b c (h w)')
        return x_out

#定义插入加速模块
class CustomViT(nn.Module):
    def __init__(self, base_model, token_select):
        super(CustomViT, self).__init__()
        self.base_model = base_model         # 存储传入的预训练ViT模型
        self.token_select = token_select     # 存储传入的TokenSelect模块

        # 插入TokenSelect模块的位置
        self.insert_pos = 1                  # 定义TokenSelect模块应该插入的位置（在第一个和第二个transformer块之间）

    def forward(self, x):
        # 处理前置层（例如：位置编码等）
        x = self.base_model.patch_embed(x)   # 将输入图片转换为patch embeddings,使得图像数据能够被Transformer处理
        cls_token = self.base_model.cls_token.expand(x.shape[0], -1, -1)  # 复制CLS token到每个样本
        x = torch.cat((cls_token, x), dim=1)  # 将CLS token附加到每个样本的patch embeddings前
        x = x + self.base_model.pos_embed     # 添加位置嵌入
        x = self.base_model.pos_drop(x)       # 应用位置dropout

        # 通过每个transformer块
        for i, blk in enumerate(self.base_model.blocks):
            x = blk(x)
            if i == self.insert_pos:
                x,_ = self.token_select(x)      # 在第一个和第二个块之间插入TokenSelect模块

        # 处理分类头
        x = self.base_model.norm(x)           # 应用Layer Normalization
        return self.base_model.head(x[:, 0])  # 取出CLS token的输出进行分类
    
class SaveOutput:
    #当创建类的实例时自动调用
    def __init__(self):
        self.outputs = [] 
    #魔术方法，使得类的实例可以像函数一样被调用
    #进行调用时触发此方法
    def __call__(self, module, module_in, module_out):
        self.outputs.append(module_out)
    # 当需要清除已保存的结果时，可以调用 clear 方法
    def clear(self):
        self.outputs = []


class MANIQA(nn.Module):
    #参数初始化，嵌入维度，输出维度，图像块大小，Transformer层的深度
    def __init__(self, embed_dim=72, num_outputs=1, patch_size=8, drop=0.1, 
                    depths=[2, 2], window_size=4, dim_mlp=768, num_heads=[4, 4],
                    img_size=224, num_tab=2, scale=0.8, **kwargs):
        super().__init__()
        self.img_size = img_size
        self.patch_size = patch_size
        self.input_size = img_size // patch_size
        self.patches_resolution = (img_size // patch_size, img_size // patch_size)

        #使用timm库加载预训练的vit模型
        # self.vit = timm.create_model('vit_base_patch8_224', pretrained=True)  ##

        # 加载预训练的ViT模型
        vit_base = timm.create_model('vit_base_patch8_224', pretrained=True)

        # 创建TokenSelect模块
        token_select = TokenSelect()  # 需要确保这个类已经被正确定义

        self.save_output = SaveOutput()
        
        # 创建模型
        self.vit = CustomViT(vit_base, token_select)

        #注册前向钩子，收集特定层的输出，用于后续的特征提取
        hook_handles = []
        for layer in self.vit.modules():
            if isinstance(layer, Block):
                handle = layer.register_forward_hook(self.save_output)
                hook_handles.append(handle)

        #self.tablock1 = nn.ModuleList()创建了一个空的ModuleList对象，用于存放TABlock模块
        self.tablock1 = nn.ModuleList()
        for i in range(num_tab):
            # tab = TABlock(self.input_size ** 2)
            tab = MultiDimensional ()
            self.tablock1.append(tab)

        self.conv1 = nn.Conv2d(embed_dim * 4, embed_dim, 1, 1, 0)
        
        self.swintransformer1 = SwinTransformer(
            patches_resolution=self.patches_resolution,
            depths=depths,
            num_heads=num_heads,
            embed_dim=embed_dim,
            window_size=window_size,
            dim_mlp=dim_mlp,
            scale=scale
        )

        self.tablock2 = nn.ModuleList()
        for i in range(num_tab):
            tab = MultiDimensional ()
            self.tablock2.append(tab)

        self.conv2 = nn.Conv2d(embed_dim, embed_dim // 2, 1, 1, 0)
        self.swintransformer2 = SwinTransformer(
            patches_resolution=self.patches_resolution,
            depths=depths,
            num_heads=num_heads,
            embed_dim=embed_dim // 2,
            window_size=window_size,
            dim_mlp=dim_mlp,
            scale=scale
        )
        
        self.fc_score = nn.Sequential(
            nn.Linear(embed_dim // 2, embed_dim // 2),
            nn.ReLU(),
            nn.Dropout(drop),
            nn.Linear(embed_dim // 2, num_outputs),
            nn.ReLU()
        )
        self.fc_weight = nn.Sequential(
            nn.Linear(embed_dim // 2, embed_dim // 2),
            nn.ReLU(),
            nn.Dropout(drop),
            nn.Linear(embed_dim // 2, num_outputs),
            nn.Sigmoid()
        )
    #聚合多层特征，从一个中间层输出的存储器对象中提取特定层的输出，并将这些输出连接起来
    def extract_feature(self, save_output):
        x6, x7, x8, x9 = [save_output.outputs[i][:, 1:] for i in range(6, 10)]
        x = torch.cat((x6,x7,x8,x9),dim=2)
        return x
    
    #定义前向传递
    def forward(self, x):
        _x = self.vit(x)
        x = self.extract_feature(self.save_output)
        self.save_output.outputs.clear()

        # stage 1
        # print(x.shape)(2,3072,28,28)

        x = rearrange(x, 'b (h w) c -> b c (h w)', h=self.input_size, w=self.input_size)
        # print(x.shape)
        # print(type(x))
        #tablock1里面是经过了TABblock的搜集块的列表
        for tab in self.tablock1:
            x = tab(x)
        x = rearrange(x, 'b c (h w) -> b c h w', h=self.input_size, w=self.input_size)
        x = self.conv1(x)
        x = self.swintransformer1(x)

        # stage2
        x = rearrange(x, 'b c h w -> b c (h w)', h=self.input_size, w=self.input_size)
        for tab in self.tablock2:
            x = tab(x)
        x = rearrange(x, 'b c (h w) -> b c h w', h=self.input_size, w=self.input_size)
        x = self.conv2(x)
        x = self.swintransformer2(x)

        x = rearrange(x, 'b c h w -> b (h w) c', h=self.input_size, w=self.input_size)
        score = torch.tensor([]).cuda()

        for i in range(x.shape[0]):
            f = self.fc_score(x[i])
            w = self.fc_weight(x[i])
            _s = torch.sum(f * w) / torch.sum(w)
            score = torch.cat((score, _s.unsqueeze(0)), 0)
        return score






